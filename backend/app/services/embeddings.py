"""Thin client for the BGE-M3 embedding model served through LM Studio.

Mirrors ingestion/embeddings.py — kept as a separate copy on purpose so the
backend service and the ingestion job have no runtime dependency on each
other and can be deployed/scaled independently. Dense-only for now; see the
docstring in ingestion/embeddings.py for the path to hybrid (dense+sparse)
retrieval.
"""

import httpx

from app.config import EMBEDDING_MODEL, LM_STUDIO_BASE_URL


async def embed_text(text: str) -> list[float]:
    async with httpx.AsyncClient(base_url=LM_STUDIO_BASE_URL, timeout=30.0) as client:
        response = await client.post(
            "/embeddings",
            json={"model": EMBEDDING_MODEL, "input": text},
        )
        response.raise_for_status()
        data = response.json()
        # OpenAI-compatible response: {"data": [{"embedding": [...], "index": 0}]}
        return data["data"][0]["embedding"]


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed several documents in one request, preserving input order.

    Used when syncing approved schema changes, where a handful of table
    documents are re-embedded together. The response is sorted by `index`
    because the OpenAI-compatible contract does not guarantee ordering.
    """
    if not texts:
        return []

    async with httpx.AsyncClient(base_url=LM_STUDIO_BASE_URL, timeout=120.0) as client:
        response = await client.post(
            "/embeddings",
            json={"model": EMBEDDING_MODEL, "input": texts},
        )
        response.raise_for_status()
        data = response.json()
        entries = sorted(data["data"], key=lambda item: item["index"])
        return [entry["embedding"] for entry in entries]
