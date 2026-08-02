"""Thin client for the BGE-M3 embedding model served through LM Studio.

BGE-M3 natively supports dense, sparse, and multi-vector (ColBERT-style)
output. LM Studio's OpenAI-compatible /v1/embeddings endpoint only exposes
the dense vector, so this client returns dense embeddings only for now.
If/when hybrid retrieval is added, this is the module to extend (e.g. swap
to a server that exposes sparse weights, such as FlagEmbedding served
directly) — callers elsewhere only depend on `embed_text` / `embed_texts`
returning a list of floats, so that change would stay isolated here.
"""

import httpx

from config import EMBEDDING_MODEL, LM_STUDIO_BASE_URL


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts, returning one dense vector per input text."""
    with httpx.Client(base_url=LM_STUDIO_BASE_URL, timeout=60.0) as client:
        response = client.post(
            "/embeddings",
            json={"model": EMBEDDING_MODEL, "input": texts},
        )
        response.raise_for_status()
        data = response.json()
        # OpenAI-compatible response: {"data": [{"embedding": [...], "index": 0}, ...]}
        ordered = sorted(data["data"], key=lambda item: item["index"])
        return [item["embedding"] for item in ordered]
