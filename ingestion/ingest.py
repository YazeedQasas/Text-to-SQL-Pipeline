"""
Build schema documents from schema_docs.py, embed them with BGE-M3, and
upsert them into the Qdrant `schema_docs` collection.

Usage:
    python ingest.py

Re-running is idempotent: point IDs are deterministic (uuid5 of the table
name), so re-ingesting after editing a description overwrites the existing
point instead of duplicating it.
"""

import uuid
from dataclasses import asdict

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from config import EMBEDDING_DIM, QDRANT_API_KEY, QDRANT_COLLECTION, QDRANT_URL
from embeddings import embed_texts
from schema_docs import DOC_LEVEL_TABLE, TABLES, build_table_doc_text

# Fixed namespace so point IDs are stable across runs/machines.
_POINT_NAMESPACE = uuid.UUID("f6a1f7d2-7e3e-4a6b-9c1d-2b6e6f6a1a10")


def _table_point_id(table_name: str) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, f"{DOC_LEVEL_TABLE}:{table_name}"))


def ensure_collection(client: QdrantClient) -> None:
    if client.collection_exists(QDRANT_COLLECTION):
        return
    client.create_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
    )


def build_table_points() -> list[PointStruct]:
    """Build one Qdrant point per table document.

    Payload is intentionally flat and self-describing so the backend's
    retrieval layer never needs to re-derive schema metadata from doc_text —
    it reads structured `columns`/`foreign_keys` directly. `level` is carried
    on every point so a future column-level collection (or a shared
    collection with mixed levels, filtered by `level == "column"`) can coexist
    without changing this ingestion path.
    """
    doc_texts = [build_table_doc_text(table) for table in TABLES]
    vectors = embed_texts(doc_texts)

    points = []
    for table, doc_text, vector in zip(TABLES, doc_texts, vectors):
        payload = {
            "level": DOC_LEVEL_TABLE,
            "table_name": table.name,
            "description": table.description,
            "primary_key": table.primary_key,
            "columns": [asdict(c) for c in table.columns],
            "foreign_keys": [asdict(fk) for fk in table.foreign_keys],
            "doc_text": doc_text,
        }
        points.append(
            PointStruct(id=_table_point_id(table.name), vector=vector, payload=payload)
        )
    return points


def build_column_documents():
    """Stub for future column-level ingestion.

    Each TableDoc.columns entry already has name/type/description — a
    column-level doc would just be
    f"Column: {table.name}.{col.name} ({col.type}) - {col.description}"
    embedded and upserted with level=DOC_LEVEL_COLUMN and a payload that
    also carries `table_name` so results can be grouped back into tables.
    Not implemented: out of scope until column-level search is needed.
    """
    raise NotImplementedError


def main() -> None:
    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    ensure_collection(client)

    points = build_table_points()
    client.upsert(collection_name=QDRANT_COLLECTION, points=points)

    print(f"Ingested {len(points)} table documents into '{QDRANT_COLLECTION}' at {QDRANT_URL}")
    for table in TABLES:
        print(f"  - {table.name}")


if __name__ == "__main__":
    main()
