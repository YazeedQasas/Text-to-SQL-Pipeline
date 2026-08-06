"""Keep data/concepts.json and the Qdrant concepts collection in step, both ways.

The problem this solves
-----------------------
Comparing two sides tells you they DIFFER; it cannot tell you which one moved.
"Removed from the file" and "added to Qdrant" produce exactly the same
two-way diff, and so do "removed from Qdrant" and "added to the file". A
two-way sync has to guess, and half its guesses delete data that was just
created.

So the sync is three-way. `.concepts-sync-state.json` records the state both
sides agreed on last time, and each concept is decided by comparing the file and
Qdrant against that snapshot rather than against each other:

    file    snapshot  qdrant   meaning                     action
    ------  --------  -------  --------------------------  ----------------------
    yes     no        no       added to the file           upsert into Qdrant
    no      no        yes      added to Qdrant             add to the file
    yes     yes       no       deleted from Qdrant         remove from the file
    no      yes       yes      deleted from the file       delete from Qdrant
    edited  yes       same     edited in the file          upsert into Qdrant
    same    yes       edited   edited in Qdrant            write back to the file
    edited  yes       edited   edited on both sides        the file wins, warn

The last row is the only genuine ambiguity, and the file wins because it is the
side a human authored deliberately — a Qdrant-side edit is almost always a
script or a dashboard poke. The losing version is reported as a conflict rather
than discarded silently.

The delete rail
---------------
The failure that actually happens is not a subtle merge error: it is Qdrant
restarting on an empty volume, every concept looking deleted, and an unguarded
reconcile emptying concepts.json to match. So a plan that would delete more than
CONCEPT_SYNC_DELETE_RATIO of either side is refused outright and raised as a
warning. It can be forced, once a human has looked at what it wanted to do.

`plan_sync` is pure and does no IO, which is what makes the table above testable
without a Qdrant or a filesystem.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import (
    CONCEPT_SYNC_DELETE_FLOOR,
    CONCEPT_SYNC_DELETE_RATIO,
    CONCEPTS_FILE,
    CONCEPTS_SYNC_STATE_FILE,
)
from app.services import activity, concept_store
from app.services.concept_docs import (
    ConceptDoc,
    build_concept_doc_text,
    concept_from_dict,
    load_concepts_file,
    save_concepts_file,
    same_content,
)
from app.services.embeddings import embed_texts

logger = logging.getLogger(__name__)

# One sync at a time. The log watcher, the file upload endpoint and a manual
# click can all fire at once, and two concurrent reconciles would each plan
# against a state the other is midway through changing.
_sync_lock = asyncio.Lock()


@dataclass
class Conflict:
    """One concept edited on both sides since the last sync."""

    concept_id: str
    file_version: dict
    qdrant_version: dict


@dataclass
class SyncPlan:
    # The file as it should be after this sync, in order.
    file_contents: list[ConceptDoc] = field(default_factory=list)
    upsert_to_qdrant: list[ConceptDoc] = field(default_factory=list)
    delete_from_qdrant: list[str] = field(default_factory=list)
    added_to_file: list[str] = field(default_factory=list)
    updated_in_file: list[str] = field(default_factory=list)
    removed_from_file: list[str] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)

    def is_noop(self) -> bool:
        return not (
            self.upsert_to_qdrant
            or self.delete_from_qdrant
            or self.added_to_file
            or self.updated_in_file
            or self.removed_from_file
        )


@dataclass
class SyncResult:
    ok: bool
    trigger: str
    upserted: list[str] = field(default_factory=list)
    deleted_from_qdrant: list[str] = field(default_factory=list)
    added_to_file: list[str] = field(default_factory=list)
    updated_in_file: list[str] = field(default_factory=list)
    removed_from_file: list[str] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    refused_reason: str = ""
    file_count: int = 0
    qdrant_count: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "trigger": self.trigger,
            "upserted": self.upserted,
            "deleted_from_qdrant": self.deleted_from_qdrant,
            "added_to_file": self.added_to_file,
            "updated_in_file": self.updated_in_file,
            "removed_from_file": self.removed_from_file,
            "conflicts": [
                {
                    "concept_id": c.concept_id,
                    "file_version": c.file_version,
                    "qdrant_version": c.qdrant_version,
                }
                for c in self.conflicts
            ],
            "refused_reason": self.refused_reason,
            "file_count": self.file_count,
            "qdrant_count": self.qdrant_count,
        }


# --- Snapshot -----------------------------------------------------------------


def load_snapshot(path: Path | None = None) -> dict[str, ConceptDoc]:
    """The state both sides agreed on at the end of the last sync.

    A missing snapshot is not an error: it makes every concept look "new on
    whichever side it is on", so the first sync after deleting it MERGES the two
    sides rather than deleting from either. That is the safe direction to fail —
    the worst case is a concept coming back, not one disappearing.
    """
    path = path or Path(CONCEPTS_SYNC_STATE_FILE)
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("concepts", []) if isinstance(data, dict) else data
        concepts = [concept_from_dict(entry) for entry in entries]
        return {concept.id: concept for concept in concepts}
    except Exception as exc:  # noqa: BLE001 — a corrupt snapshot degrades to none
        logger.warning("Could not read the concept sync snapshot at %s: %s", path, exc)
        return {}


def save_snapshot(concepts: list[ConceptDoc], path: Path | None = None) -> None:
    path = path or Path(CONCEPTS_SYNC_STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {
            "synced_at": time.time(),
            "_comment": "Written by concept_sync.py. Do not edit — see data/CONCEPTS.md.",
            "concepts": [concept.to_dict() for concept in concepts],
        },
        ensure_ascii=False,
        indent=2,
    )
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(body + "\n", encoding="utf-8")
    temp_path.replace(path)


# --- Planning (pure) ----------------------------------------------------------


def plan_sync(
    file_concepts: list[ConceptDoc],
    snapshot: dict[str, ConceptDoc],
    qdrant_concepts: dict[str, ConceptDoc],
) -> SyncPlan:
    """Decide what each side needs, without touching either. See the module docstring."""
    plan = SyncPlan()

    file_by_id = {concept.id: concept for concept in file_concepts}
    # `resolved` holds the surviving version of every concept that should end up
    # in the file, or nothing at all if it should not.
    resolved: dict[str, ConceptDoc] = {}

    for concept_id in sorted(set(file_by_id) | set(snapshot) | set(qdrant_concepts)):
        in_file = file_by_id.get(concept_id)
        in_snapshot = snapshot.get(concept_id)
        in_qdrant = qdrant_concepts.get(concept_id)

        # --- Added on one side, unknown to the other two ----------------------
        if in_file and not in_snapshot and not in_qdrant:
            resolved[concept_id] = in_file
            plan.upsert_to_qdrant.append(in_file)
            continue

        if in_qdrant and not in_snapshot and not in_file:
            resolved[concept_id] = in_qdrant
            plan.added_to_file.append(concept_id)
            continue

        # --- Deleted on one side ---------------------------------------------
        if in_snapshot and in_file and not in_qdrant:
            # Gone from Qdrant since the last sync — drop it from the file too.
            plan.removed_from_file.append(concept_id)
            continue

        if in_snapshot and in_qdrant and not in_file:
            # Gone from the file since the last sync — delete the point.
            plan.delete_from_qdrant.append(concept_id)
            continue

        if in_snapshot and not in_file and not in_qdrant:
            # Already gone from both. Nothing to do beyond letting it fall out
            # of the new snapshot.
            continue

        # --- Present on both sides -------------------------------------------
        if in_file and in_qdrant:
            if in_snapshot is None:
                # No record of a previous agreement — the snapshot was deleted or
                # this is the first run. Identical versions just get adopted.
                if same_content(in_file, in_qdrant):
                    resolved[concept_id] = in_file
                else:
                    resolved[concept_id] = in_file
                    plan.upsert_to_qdrant.append(in_file)
                    plan.conflicts.append(
                        Conflict(concept_id, in_file.to_dict(), in_qdrant.to_dict())
                    )
                continue

            file_edited = not same_content(in_file, in_snapshot)
            qdrant_edited = not same_content(in_qdrant, in_snapshot)

            if file_edited and qdrant_edited:
                if same_content(in_file, in_qdrant):
                    # Both moved to the same place. Nothing to reconcile.
                    resolved[concept_id] = in_file
                else:
                    # The genuine ambiguity. The file wins — it is the side a
                    # human authored — and the Qdrant version is reported rather
                    # than dropped silently.
                    resolved[concept_id] = in_file
                    plan.upsert_to_qdrant.append(in_file)
                    plan.conflicts.append(
                        Conflict(concept_id, in_file.to_dict(), in_qdrant.to_dict())
                    )
            elif file_edited:
                resolved[concept_id] = in_file
                plan.upsert_to_qdrant.append(in_file)
            elif qdrant_edited:
                resolved[concept_id] = in_qdrant
                plan.updated_in_file.append(concept_id)
            else:
                resolved[concept_id] = in_file

    # Preserve the order the user has their file in, and append anything adopted
    # from Qdrant at the end — reordering a file someone maintains by hand makes
    # every sync look like a rewrite in their diff.
    ordered: list[ConceptDoc] = [
        resolved[concept.id] for concept in file_concepts if concept.id in resolved
    ]
    ordered.extend(
        resolved[concept_id] for concept_id in plan.added_to_file if concept_id in resolved
    )
    plan.file_contents = ordered

    return plan


def exceeds_delete_rail(delete_count: int, total: int) -> bool:
    """Whether deleting this many out of this many is too much to do unattended.

    The floor is what keeps small collections editable: removing 1 concept of 3
    is 33% but obviously fine, and a bare ratio would make a short file
    impossible to shrink.
    """
    if delete_count < CONCEPT_SYNC_DELETE_FLOOR or total <= 0:
        return False
    return delete_count > CONCEPT_SYNC_DELETE_RATIO * total


def check_delete_rail(plan: SyncPlan, file_total: int, qdrant_total: int) -> str:
    """The reason to refuse this plan, or "" to proceed."""
    if exceeds_delete_rail(len(plan.delete_from_qdrant), qdrant_total):
        return (
            f"Refused: this sync would delete {len(plan.delete_from_qdrant)} of "
            f"{qdrant_total} concepts from Qdrant. Check that concepts.json is the "
            f"version you meant, then force the sync to proceed."
        )
    if exceeds_delete_rail(len(plan.removed_from_file), file_total):
        return (
            f"Refused: this sync would remove {len(plan.removed_from_file)} of "
            f"{file_total} concepts from concepts.json. That usually means Qdrant "
            f"came up empty rather than that anything was really deleted — check "
            f"the collection, then force the sync to proceed."
        )
    return ""


# --- Applying -----------------------------------------------------------------


async def sync(trigger: str = "manual", force: bool = False) -> SyncResult:
    """Reconcile the file and Qdrant, and record what happened.

    `force` skips the delete rail only. It does not change how anything is
    decided — a forced sync applies exactly the plan that was refused.
    """
    async with _sync_lock:
        return await _sync_unlocked(trigger, force)


async def _sync_unlocked(trigger: str, force: bool) -> SyncResult:
    file_path = Path(CONCEPTS_FILE)

    try:
        file_concepts = load_concepts_file(file_path)
    except Exception as exc:  # noqa: BLE001 — a bad file is a user error, not a crash
        message = f"Could not read {file_path.name}: {exc}"
        activity.record(
            activity.SOURCE_CONCEPTS,
            "sync_failed",
            message,
            level=activity.LEVEL_ERROR,
            trigger=trigger,
        )
        return SyncResult(ok=False, trigger=trigger, refused_reason=message)

    try:
        qdrant_concepts = await concept_store.fetch_concepts()
    except Exception as exc:  # noqa: BLE001
        message = f"Could not read the concepts collection from Qdrant: {exc}"
        activity.record(
            activity.SOURCE_CONCEPTS,
            "sync_failed",
            message,
            level=activity.LEVEL_ERROR,
            trigger=trigger,
        )
        return SyncResult(ok=False, trigger=trigger, refused_reason=message)

    snapshot = load_snapshot()
    plan = plan_sync(file_concepts, snapshot, qdrant_concepts)

    result = SyncResult(
        ok=True,
        trigger=trigger,
        upserted=[concept.id for concept in plan.upsert_to_qdrant],
        deleted_from_qdrant=list(plan.delete_from_qdrant),
        added_to_file=list(plan.added_to_file),
        updated_in_file=list(plan.updated_in_file),
        removed_from_file=list(plan.removed_from_file),
        conflicts=list(plan.conflicts),
        file_count=len(plan.file_contents),
        qdrant_count=len(qdrant_concepts),
    )

    if plan.is_noop():
        # Still refresh the snapshot: a no-op plan means the two sides agree, and
        # recording that agreement is what lets the NEXT edit be classified.
        save_snapshot(plan.file_contents)
        return result

    if not force:
        refusal = check_delete_rail(plan, len(file_concepts), len(qdrant_concepts))
        if refusal:
            result.ok = False
            result.refused_reason = refusal
            activity.record(
                activity.SOURCE_CONCEPTS,
                "sync_refused",
                refusal,
                level=activity.LEVEL_WARNING,
                trigger=trigger,
                would_delete_from_qdrant=plan.delete_from_qdrant,
                would_remove_from_file=plan.removed_from_file,
            )
            return result

    try:
        await _apply(plan, file_path)
    except Exception as exc:  # noqa: BLE001
        message = f"Sync failed while writing: {exc}"
        logger.exception("Concept sync failed")
        activity.record(
            activity.SOURCE_CONCEPTS,
            "sync_failed",
            message,
            level=activity.LEVEL_ERROR,
            trigger=trigger,
        )
        result.ok = False
        result.refused_reason = message
        return result

    _record_success(result, forced=force)
    return result


async def _apply(plan: SyncPlan, file_path: Path) -> None:
    """Write the plan out. Qdrant first, then the file, then the snapshot.

    Order matters for crash safety. The snapshot is written LAST, so a crash
    partway through leaves it describing the previous agreed state — the next
    sync re-derives the same plan and re-applies it. Writing the snapshot first
    would make a crash look like "already synced" and lose the change.
    """
    if plan.upsert_to_qdrant:
        await concept_store.ensure_collection()
        doc_texts = [build_concept_doc_text(concept) for concept in plan.upsert_to_qdrant]
        vectors = await embed_texts(doc_texts)
        _assert_embedding_dim(vectors)
        await concept_store.upsert_concepts(plan.upsert_to_qdrant, vectors)

    if plan.delete_from_qdrant:
        await concept_store.delete_concepts(plan.delete_from_qdrant)

    if plan.added_to_file or plan.updated_in_file or plan.removed_from_file:
        save_concepts_file(file_path, plan.file_contents)

    save_snapshot(plan.file_contents)

    # The query pipeline caches the glossary for CONCEPT_CACHE_TTL_SECONDS.
    # Without this a concept edited now would not affect answers for another few
    # minutes, which reads as "the sync did nothing".
    await _refresh_query_cache()


def _assert_embedding_dim(vectors: list[list[float]]) -> None:
    """Fail before writing rather than after.

    A dimension mismatch creates points the query pipeline cannot search, and
    fixing it means deleting the whole collection — so it is worth one check.
    """
    from app.config import EMBEDDING_DIM

    if vectors and len(vectors[0]) != EMBEDDING_DIM:
        raise ValueError(
            f"Embedding model returned {len(vectors[0])}-dim vectors but EMBEDDING_DIM "
            f"is {EMBEDDING_DIM}. Fix the config (or the loaded model) before syncing."
        )


async def _refresh_query_cache() -> None:
    try:
        from app.services import retrieval

        await retrieval.load_all_concepts(force=True)
    except Exception as exc:  # noqa: BLE001 — a stale cache is not worth failing a good sync
        logger.warning("Could not refresh the concept cache after sync: %s", exc)


def _record_success(result: SyncResult, forced: bool) -> None:
    parts = []
    if result.upserted:
        parts.append(f"{len(result.upserted)} → Qdrant")
    if result.deleted_from_qdrant:
        parts.append(f"{len(result.deleted_from_qdrant)} deleted from Qdrant")
    if result.added_to_file:
        parts.append(f"{len(result.added_to_file)} → concepts.json")
    if result.updated_in_file:
        parts.append(f"{len(result.updated_in_file)} updated in concepts.json")
    if result.removed_from_file:
        parts.append(f"{len(result.removed_from_file)} removed from concepts.json")

    activity.record(
        activity.SOURCE_CONCEPTS,
        "sync_applied",
        ("Forced concept sync: " if forced else "Concept sync: ") + ", ".join(parts),
        level=activity.LEVEL_INFO,
        trigger=result.trigger,
        **{
            key: value
            for key, value in result.to_dict().items()
            if key in {"upserted", "deleted_from_qdrant", "added_to_file", "updated_in_file", "removed_from_file"}
        },
    )

    for conflict in result.conflicts:
        activity.record(
            activity.SOURCE_CONCEPTS,
            "sync_conflict",
            (
                f"'{conflict.concept_id}' was edited in both concepts.json and Qdrant. "
                f"The concepts.json version was kept."
            ),
            level=activity.LEVEL_WARNING,
            concept_id=conflict.concept_id,
            file_version=conflict.file_version,
            qdrant_version=conflict.qdrant_version,
        )
