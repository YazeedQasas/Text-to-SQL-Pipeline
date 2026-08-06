"""
Sync data/concepts.json into the Qdrant concepts collection from the command line.

Usage:
    python ingest_concepts.py            # reconcile, refusing a large deletion
    python ingest_concepts.py --force    # reconcile, ignoring the delete rail
    python ingest_concepts.py --dry-run  # show what would change, touch nothing

This is a thin wrapper around the backend's concept_sync service, not a second
implementation. The old version of this script embedded a Python list of
concepts and pushed it one way into Qdrant; the sync is now bidirectional and
the source of truth is data/concepts.json, so the same code has to run whether
the trigger is this script, an upload from the browser, or the watcher noticing
a deletion. Two implementations would eventually disagree about what "deleted"
means, and one of them would be the one that runs unattended.

The prose that used to head the old concepts.py — the field contract, what may
and may not go in `sql`, and the REVIEW REQUIRED note — is in data/CONCEPTS.md.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# The sync lives in the backend package. Adding it to the path rather than
# duplicating it here is the point of this script; see the module docstring.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "backend"))

from app.config import CONCEPTS_FILE  # noqa: E402
from app.services import concept_store, concept_sync  # noqa: E402
from app.services.concept_docs import load_concepts_file  # noqa: E402


def _print_plan(plan: concept_sync.SyncPlan) -> None:
    rows = [
        ("upsert into Qdrant", [c.id for c in plan.upsert_to_qdrant]),
        ("delete from Qdrant", plan.delete_from_qdrant),
        ("add to concepts.json", plan.added_to_file),
        ("update in concepts.json", plan.updated_in_file),
        ("remove from concepts.json", plan.removed_from_file),
    ]
    for label, ids in rows:
        if ids:
            print(f"  {label} ({len(ids)}): {', '.join(ids)}")

    for conflict in plan.conflicts:
        print(f"  CONFLICT on '{conflict.concept_id}' — concepts.json wins")

    if plan.is_noop():
        print("  nothing to do — concepts.json and Qdrant already agree")


async def _dry_run() -> int:
    file_concepts = load_concepts_file(Path(CONCEPTS_FILE))
    snapshot = concept_sync.load_snapshot()
    qdrant_concepts = await concept_store.fetch_concepts()

    plan = concept_sync.plan_sync(file_concepts, snapshot, qdrant_concepts)

    print(f"concepts.json: {len(file_concepts)}   Qdrant: {len(qdrant_concepts)}   "
          f"snapshot: {len(snapshot)}")
    print("\nWould:")
    _print_plan(plan)

    refusal = concept_sync.check_delete_rail(plan, len(file_concepts), len(qdrant_concepts))
    if refusal:
        print(f"\n{refusal}")
        return 1
    return 0


async def _sync(force: bool) -> int:
    result = await concept_sync.sync(trigger="cli", force=force)

    if not result.ok:
        print(result.refused_reason, file=sys.stderr)
        return 1

    print(f"concepts.json: {result.file_count}   Qdrant: {result.qdrant_count}")
    for label, ids in [
        ("upserted into Qdrant", result.upserted),
        ("deleted from Qdrant", result.deleted_from_qdrant),
        ("added to concepts.json", result.added_to_file),
        ("updated in concepts.json", result.updated_in_file),
        ("removed from concepts.json", result.removed_from_file),
    ]:
        if ids:
            print(f"  {label} ({len(ids)}): {', '.join(ids)}")

    for conflict in result.conflicts:
        print(
            f"  CONFLICT: '{conflict.concept_id}' was edited on both sides; "
            f"the concepts.json version was kept."
        )

    if not any(
        [
            result.upserted,
            result.deleted_from_qdrant,
            result.added_to_file,
            result.updated_in_file,
            result.removed_from_file,
        ]
    ):
        print("  nothing to do — already in sync")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="apply even if the sync wants to delete a large fraction of either side",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without writing anything",
    )
    args = parser.parse_args()

    if args.dry_run:
        raise SystemExit(asyncio.run(_dry_run()))
    raise SystemExit(asyncio.run(_sync(force=args.force)))


if __name__ == "__main__":
    main()
