"""Read, replace and reconcile the legal-concept glossary.

`data/concepts.json` is the file a user edits; these endpoints exist so they can
do it from the browser instead of on the server's filesystem, and so a sync can
be triggered on demand rather than only when the Qdrant watcher notices
something.

Uploading replaces the file and immediately reconciles. That is the whole
interaction the brief asks for — "input the concepts in a JSON file and send
that file" — and it goes through exactly the same three-way sync as every other
path, so an upload that would delete most of the glossary hits the same rail.
"""

import logging
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.config import CONCEPTS_FILE
from app.models import (
    ConceptModel,
    ConceptsResponse,
    ConceptSyncResponse,
    ConceptUploadRequest,
)
from app.services import concept_store, concept_sync
from app.services.concept_docs import (
    ConceptDoc,
    load_concepts_file,
    save_concepts_file,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/concepts", tags=["concepts"])


def _result_response(result: concept_sync.SyncResult) -> ConceptSyncResponse:
    return ConceptSyncResponse(**result.to_dict())


@router.get("", response_model=ConceptsResponse)
async def list_concepts() -> ConceptsResponse:
    """The glossary as the file has it, alongside how many points Qdrant holds."""
    try:
        concepts = load_concepts_file(Path(CONCEPTS_FILE))
    except Exception as exc:  # noqa: BLE001 — a malformed file is a user error
        raise HTTPException(status_code=422, detail=f"Could not read concepts.json: {exc}") from exc

    try:
        qdrant_concepts = await concept_store.fetch_concepts()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Could not read from Qdrant: {exc}") from exc

    return ConceptsResponse(
        concepts=[ConceptModel(**concept.to_dict()) for concept in concepts],
        qdrant_count=len(qdrant_concepts),
        in_sync=len(concepts) == len(qdrant_concepts),
    )


@router.post("/sync", response_model=ConceptSyncResponse)
async def sync_concepts(force: bool = False) -> ConceptSyncResponse:
    """Reconcile the file and Qdrant now.

    Returns 200 even when the sync was refused: a refusal is a result the user
    has to read and act on, not a transport error. `ok` and `refused_reason` on
    the body carry it.
    """
    result = await concept_sync.sync(trigger="manual", force=force)
    return _result_response(result)


@router.put("", response_model=ConceptSyncResponse)
async def replace_concepts(request: ConceptUploadRequest) -> ConceptSyncResponse:
    """Replace concepts.json wholesale, then reconcile.

    The file is written before the sync runs so that the sync sees it as the
    file side of the three-way diff — which is what makes an upload that drops
    concepts read as "deleted from the file" and delete them from Qdrant, rather
    than as an unrelated state the sync would have to guess about.
    """
    concepts = [ConceptDoc(**concept.model_dump()) for concept in request.concepts]

    counts = Counter(concept.id for concept in concepts)
    duplicates = sorted(concept_id for concept_id, n in counts.items() if n > 1)
    if duplicates:
        raise HTTPException(
            status_code=422,
            detail=f"Duplicate concept ids: {', '.join(duplicates)}",
        )

    try:
        save_concepts_file(Path(CONCEPTS_FILE), concepts)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Could not write concepts.json: {exc}") from exc

    result = await concept_sync.sync(trigger="upload", force=request.force)
    return _result_response(result)
