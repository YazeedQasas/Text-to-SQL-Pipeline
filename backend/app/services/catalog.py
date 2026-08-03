"""Read and write the indexed schema documents in Qdrant.

This is the write half of what `ingestion/ingest.py` does offline, exposed to
the backend so the catalog review flow can sync approved changes without
anyone editing `ingestion/schema_docs.py` by hand.

IMPORTANT: `_table_point_id` must stay byte-identical to the one in
ingestion/ingest.py — same UUID namespace, same key format. If it drifts,
approving a change *inserts a second point* for a table that is already
indexed instead of overwriting it, and the collection silently accumulates
duplicate (and contradictory) schema docs.
"""

import uuid

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from app.config import EMBEDDING_DIM, QDRANT_API_KEY, QDRANT_COLLECTION, QDRANT_URL

DOC_LEVEL_TABLE = "table"

# Must match ingestion/ingest.py::_POINT_NAMESPACE exactly.
_POINT_NAMESPACE = uuid.UUID("f6a1f7d2-7e3e-4a6b-9c1d-2b6e6f6a1a10")

_client = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)


def table_point_id(table_name: str) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, f"{DOC_LEVEL_TABLE}:{table_name}"))


def build_doc_text(doc: dict) -> str:
    """Render an indexed table payload into the text blob that gets embedded.

    Mirrors ingestion/schema_docs.py::build_table_doc_text, but operates on the
    plain payload dicts this module deals in rather than dataclasses. Minor
    wording drift between the two is harmless: doc_text is regenerated on every
    upsert and is never compared during a diff.
    """
    lines = [
        f"Table: {doc['table_name']}",
        f"Description: {doc['description']}",
        "Columns:",
    ]
    for column in doc.get("columns", []):
        nullability = "NULL" if column.get("nullable") else "NOT NULL"
        lines.append(
            f"  - {column['name']} ({column['type']}, {nullability}): "
            f"{column.get('description', '')}"
        )

    lines.append(f"Primary key: {doc.get('primary_key', '')}")

    foreign_keys = doc.get("foreign_keys", [])
    if foreign_keys:
        lines.append("Foreign keys:")
        for fk in foreign_keys:
            lines.append(
                f"  - {doc['table_name']}.{fk['column']} -> "
                f"{fk['references_table']}.{fk['references_column']}"
            )
    else:
        lines.append("Foreign keys: none")

    return "\n".join(lines)


async def ensure_collection() -> None:
    if await _client.collection_exists(QDRANT_COLLECTION):
        return
    await _client.create_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
    )


async def fetch_indexed_tables() -> dict[str, dict]:
    """Return every indexed table document, keyed by table name.

    These payloads double as the "schema as of last ingestion" record that
    schema_diff.py compares live MySQL against.
    """
    indexed: dict[str, dict] = {}
    offset = None

    while True:
        points, offset = await _client.scroll(
            collection_name=QDRANT_COLLECTION,
            scroll_filter=Filter(
                must=[FieldCondition(key="level", match=MatchValue(value=DOC_LEVEL_TABLE))]
            ),
            limit=256,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        for point in points:
            payload = point.payload or {}
            table_name = payload.get("table_name")
            if table_name:
                indexed[table_name] = payload
        if offset is None:
            break

    return indexed


async def upsert_tables(docs: list[dict], vectors: list[list[float]]) -> None:
    """Write approved table documents, overwriting any existing point per table."""
    if not docs:
        return

    points = [
        PointStruct(
            id=table_point_id(doc["table_name"]),
            vector=vector,
            payload={**doc, "level": DOC_LEVEL_TABLE, "doc_text": build_doc_text(doc)},
        )
        for doc, vector in zip(docs, vectors)
    ]
    await _client.upsert(collection_name=QDRANT_COLLECTION, points=points)


async def delete_tables(table_names: list[str]) -> None:
    """Remove table documents whose underlying MySQL table no longer exists."""
    if not table_names:
        return
    await _client.delete(
        collection_name=QDRANT_COLLECTION,
        points_selector=[table_point_id(name) for name in table_names],
    )
