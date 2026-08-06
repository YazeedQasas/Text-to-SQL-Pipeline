"""The concept record: its JSON file, its Qdrant payload, and the text embedded.

`data/concepts.json` is the file a user edits; the `legal_concepts` Qdrant
collection is what the query pipeline actually reads. This module is the
translation layer between them, and nothing else knows the shape of either.

The authoring rules — what each field means, why `sql` is never embedded — are
in `data/CONCEPTS.md`, which replaced the prose header of the old
`ingestion/concepts.py`.

IMPORTANT: `concept_point_id` must stay byte-identical to the one the old
ingestion script used — same UUID namespace, same key format. If it drifts,
syncing inserts a *second* point for a concept that is already there instead of
overwriting it, and the collection quietly accumulates duplicate definitions.
This mirrors the same warning on catalog.py::table_point_id.
"""

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Must match the namespace the original ingestion/ingest_concepts.py used, and
# stay distinct from catalog.py's — these are different documents in a different
# collection, and a shared namespace would only invite an id collision if the
# two were ever merged.
_POINT_NAMESPACE = uuid.UUID("b2c4d6e8-1a3c-4e5f-8a9b-0c1d2e3f4a5b")

# The fields that define a concept's content. Anything outside this set (the
# derived `doc_text`, Qdrant's own bookkeeping) is excluded from equality, so a
# change in how doc_text renders never registers as an edit needing a sync.
CONTENT_FIELDS = ("term", "aliases", "definition", "sql", "tables")


@dataclass
class ConceptDoc:
    id: str
    term: str
    definition: str
    sql: str
    tables: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """The JSON-file shape. Key order is explicit rather than the dataclass
        field order, so the file stays readable: identity, then the Arabic a
        user actually edits, then the SQL wiring."""
        return {
            "id": self.id,
            "term": self.term,
            "aliases": self.aliases,
            "definition": self.definition,
            "sql": self.sql,
            "tables": self.tables,
        }


def concept_point_id(concept_id: str) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, f"concept:{concept_id}"))


def build_concept_doc_text(concept: ConceptDoc) -> str:
    """Render a concept into the text blob that gets embedded.

    Deliberately excludes `sql` and `tables`. The blob is matched against an
    Arabic question, so it holds only the Arabic surface forms a user might
    type — the term, the ways it is otherwise said, and what it means. Adding
    the SQL fragment here would pull the vector toward identifier soup and away
    from the question.

    Aliases come before the definition because they carry more matching signal
    per token: a user asking "كم قضية أمام محاكم الصلح" has written an alias
    almost verbatim, while the definition matches only loosely.

    PENDING EXPERIMENT — add a sample question per concept.
    Measured against a 13-question probe, true matches bottom out at 0.5605
    (محاكم الصلح) while the worst false positive reaches 0.5149 ("ما هي عناوين
    القضايا المدنية؟" pulling القضايا البسيطة). A 0.046 margin is too thin to
    trust. A question embeds closer to another question than a definition does,
    so adding one sample question per concept to this blob should lift the true
    matches without lifting the false ones. Measure the gap before and after —
    if it does not widen, drop the idea rather than carrying the extra field.
    """
    lines = [f"المصطلح: {concept.term}"]
    if concept.aliases:
        lines.append(f"ويُسمّى أيضًا: {'، '.join(concept.aliases)}")
    lines.append(f"التعريف: {concept.definition}")
    return "\n".join(lines)


def _as_str_list(value: object) -> list[str]:
    """Coerce a hand-edited field into a list of strings.

    `concepts.json` is edited by people, and writing `"tables": "courts"` where a
    list belongs is the mistake they actually make. Accepting it beats failing
    the whole sync over one entry.
    """
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def concept_from_dict(raw: dict) -> ConceptDoc:
    """Build a ConceptDoc from a JSON entry or a Qdrant payload.

    Both sides use the same field names, so one parser covers both. The Qdrant
    payload additionally carries `concept_id` (its key) and `doc_text` (derived);
    the former is accepted as an alias for `id`, and the latter ignored.
    """
    concept_id = str(raw.get("id") or raw.get("concept_id") or "").strip()
    if not concept_id:
        raise ValueError(f"Concept is missing an 'id': {raw!r}")

    return ConceptDoc(
        id=concept_id,
        term=str(raw.get("term") or "").strip(),
        definition=str(raw.get("definition") or "").strip(),
        sql=str(raw.get("sql") or "").strip(),
        tables=_as_str_list(raw.get("tables")),
        aliases=_as_str_list(raw.get("aliases")),
    )


def same_content(left: ConceptDoc, right: ConceptDoc) -> bool:
    """Whether two versions of a concept say the same thing.

    Compares CONTENT_FIELDS only. `id` is what pairs them up in the first place,
    and the derived doc_text is regenerated on every write.
    """
    return all(getattr(left, name) == getattr(right, name) for name in CONTENT_FIELDS)


def to_payload(concept: ConceptDoc) -> dict:
    """The Qdrant payload for one concept.

    `concept_id` rather than `id` is the key, because Qdrant already uses `id`
    for the point itself. `doc_text` stores exactly what was embedded, so a
    surprising match can be explained later without re-deriving it.
    """
    return {
        "concept_id": concept.id,
        "term": concept.term,
        "aliases": concept.aliases,
        "definition": concept.definition,
        "sql": concept.sql,
        "tables": concept.tables,
        "doc_text": build_concept_doc_text(concept),
    }


def parse_concepts_file(text: str) -> list[ConceptDoc]:
    """Parse the contents of concepts.json.

    Accepts either the documented `{"concepts": [...]}` wrapper or a bare list,
    since a user hand-assembling a file to upload will produce both.
    """
    data = json.loads(text)
    entries = data.get("concepts", []) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError("concepts.json must hold a list of concepts")

    concepts = [concept_from_dict(entry) for entry in entries]

    seen: set[str] = set()
    duplicates: set[str] = set()
    for concept in concepts:
        if concept.id in seen:
            duplicates.add(concept.id)
        seen.add(concept.id)
    if duplicates:
        raise ValueError(f"Duplicate concept ids in concepts.json: {', '.join(sorted(duplicates))}")

    return concepts


def load_concepts_file(path: Path) -> list[ConceptDoc]:
    """Read concepts.json, treating a missing file as an empty list.

    A missing file is NOT an error here — but see concept_sync, where an empty
    JSON side against a populated Qdrant trips the delete rail rather than
    emptying the collection.
    """
    if not path.exists():
        return []
    return parse_concepts_file(path.read_text(encoding="utf-8"))


def save_concepts_file(path: Path, concepts: list[ConceptDoc]) -> None:
    """Write concepts.json atomically, preserving the order given.

    Written to a temporary file and renamed, so a crash mid-write cannot leave a
    truncated concepts.json — this file is a source of truth that a user edits by
    hand, and half of it is worse than none of it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {"concepts": [concept.to_dict() for concept in concepts]},
        ensure_ascii=False,
        indent=2,
    )

    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(body + "\n", encoding="utf-8")
    temp_path.replace(path)
