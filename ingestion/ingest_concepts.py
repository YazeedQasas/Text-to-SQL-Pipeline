"""
Build domain-concept documents from concepts.py, embed them with BGE-M3, and
upsert them into the Qdrant concepts collection.

Usage:
    python ingest_concepts.py

Re-running is idempotent: point IDs are deterministic (uuid5 of the concept's
`id` slug), so re-ingesting after editing a definition overwrites the existing
point instead of duplicating it. This is why ConceptDoc carries an `id`
separate from `term` — fixing a typo in the Arabic term must not orphan the
old point and create a second one.

Deletions are NOT automatic. Removing a concept from concepts.py leaves its
point in Qdrant, where it keeps matching questions. `--prune` deletes the
points that no longer correspond to a concept in the file.
"""

import argparse
import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from concepts import CONCEPTS, build_concept_doc_text
from config import (
    EMBEDDING_DIM,
    QDRANT_API_KEY,
    QDRANT_CONCEPTS_COLLECTION,
    QDRANT_URL,
)
from embeddings import embed_texts

# Distinct from the schema-docs namespace in ingest.py: these are different
# documents in a different collection, and a shared namespace would only invite
# an id collision if the two ever merged.
_POINT_NAMESPACE = uuid.UUID("b2c4d6e8-1a3c-4e5f-8a9b-0c1d2e3f4a5b")


def _concept_point_id(concept_id: str) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, f"concept:{concept_id}"))


def ensure_collection(client: QdrantClient) -> None:
    if client.collection_exists(QDRANT_CONCEPTS_COLLECTION):
        return
    client.create_collection(
        collection_name=QDRANT_CONCEPTS_COLLECTION,
        vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
    )


def build_concept_points() -> list[PointStruct]:
    """Build one Qdrant point per concept.

    Only `term`, `aliases` and `definition` reach the vector (via
    `build_concept_doc_text`). `sql` and `tables` ride in the payload alone —
    embedding SQL would drag the vector toward identifier soup and away from
    the Arabic question it exists to match.

    `doc_text` stores exactly what was embedded, so a surprising match can be
    explained later without re-deriving it from the other fields.
    """
    doc_texts = [build_concept_doc_text(concept) for concept in CONCEPTS]
    vectors = embed_texts(doc_texts)

    # A dimension mismatch here creates a collection the backend cannot query,
    # and fixing it means deleting the collection — so fail loudly instead.
    actual_dim = len(vectors[0])
    if actual_dim != EMBEDDING_DIM:
        raise SystemExit(
            f"Embedding model returned {actual_dim}-dim vectors but EMBEDDING_DIM "
            f"is {EMBEDDING_DIM}. Fix the config (or the loaded model) before ingesting."
        )

    points = []
    for concept, doc_text, vector in zip(CONCEPTS, doc_texts, vectors):
        payload = {
            "concept_id": concept.id,
            "term": concept.term,
            "aliases": concept.aliases,
            "definition": concept.definition,
            "sql": concept.sql,
            "tables": concept.tables,
            "doc_text": doc_text,
        }
        points.append(
            PointStruct(id=_concept_point_id(concept.id), vector=vector, payload=payload)
        )
    return points


def prune_removed(client: QdrantClient, keep_ids: set[str]) -> list[str]:
    """Delete points whose concept no longer exists in concepts.py."""
    stale_points, stale_names = [], []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=QDRANT_CONCEPTS_COLLECTION,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            concept_id = (point.payload or {}).get("concept_id")
            if concept_id not in keep_ids:
                stale_points.append(point.id)
                stale_names.append(concept_id or str(point.id))
        if offset is None:
            break

    if stale_points:
        client.delete(collection_name=QDRANT_CONCEPTS_COLLECTION, points_selector=stale_points)
    return stale_names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prune",
        action="store_true",
        help="delete points for concepts no longer present in concepts.py",
    )
    args = parser.parse_args()

    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    ensure_collection(client)

    points = build_concept_points()
    client.upsert(collection_name=QDRANT_CONCEPTS_COLLECTION, points=points)

    print(
        f"Ingested {len(points)} concepts into "
        f"'{QDRANT_CONCEPTS_COLLECTION}' at {QDRANT_URL}"
    )
    for concept in CONCEPTS:
        aliases = f" ({', '.join(concept.aliases)})" if concept.aliases else ""
        print(f"  - {concept.term}{aliases}")

    if args.prune:
        removed = prune_removed(client, {concept.id for concept in CONCEPTS})
        if removed:
            print(f"\nPruned {len(removed)} stale point(s): {', '.join(removed)}")
        else:
            print("\nNothing to prune.")


if __name__ == "__main__":
    main()
