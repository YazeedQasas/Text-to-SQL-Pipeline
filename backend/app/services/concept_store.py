"""Read and write the legal-concept points in Qdrant.

The write half of what `ingestion/ingest_concepts.py` used to do offline, moved
into the backend so the file sync can run unattended. Mirrors the shape of
catalog.py, which does the same job for schema documents.

Deliberately a separate collection from schema_docs rather than a
`level`-filtered slice of it: the glossary is hand-authored and re-synced on its
own cadence, while schema_docs is also written by the CDC path. Keeping them
apart means editing a definition can never disturb a table document.
"""

import logging

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.config import (
    EMBEDDING_DIM,
    QDRANT_API_KEY,
    QDRANT_CONCEPTS_COLLECTION,
    QDRANT_URL,
)
from app.services.concept_docs import (
    ConceptDoc,
    concept_from_dict,
    concept_point_id,
    to_payload,
)

logger = logging.getLogger(__name__)

_client = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)


async def collection_exists() -> bool:
    return await _client.collection_exists(QDRANT_CONCEPTS_COLLECTION)


async def ensure_collection() -> None:
    if await collection_exists():
        return
    await _client.create_collection(
        collection_name=QDRANT_CONCEPTS_COLLECTION,
        vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
    )


async def fetch_concepts() -> dict[str, ConceptDoc]:
    """Every concept currently in Qdrant, keyed by concept_id.

    A missing collection returns {} rather than raising — that is the state
    before the first sync, and the sync handles it as "Qdrant has nothing yet".
    """
    if not await collection_exists():
        return {}

    concepts: dict[str, ConceptDoc] = {}
    offset = None

    while True:
        points, offset = await _client.scroll(
            collection_name=QDRANT_CONCEPTS_COLLECTION,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            try:
                concept = concept_from_dict(payload)
            except ValueError:
                # A point with no concept_id cannot be paired with a file entry,
                # so the sync has nothing to say about it. Left in place rather
                # than deleted: it was put there by something, and this is not
                # the code that gets to decide it was a mistake.
                logger.warning("Skipping concept point with no concept_id: %s", point.id)
                continue
            concepts[concept.id] = concept
        if offset is None:
            break

    return concepts


async def upsert_concepts(concepts: list[ConceptDoc], vectors: list[list[float]]) -> None:
    """Write concepts, overwriting any existing point with the same concept id."""
    if not concepts:
        return

    points = [
        PointStruct(id=concept_point_id(concept.id), vector=vector, payload=to_payload(concept))
        for concept, vector in zip(concepts, vectors)
    ]
    await _client.upsert(collection_name=QDRANT_CONCEPTS_COLLECTION, points=points)


async def delete_concepts(concept_ids: list[str]) -> None:
    if not concept_ids:
        return
    await _client.delete(
        collection_name=QDRANT_CONCEPTS_COLLECTION,
        points_selector=[concept_point_id(concept_id) for concept_id in concept_ids],
    )
