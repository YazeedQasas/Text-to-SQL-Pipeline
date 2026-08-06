"""Schema catalog review endpoints.

Two steps: scan detects drift between live MySQL and what is indexed in Qdrant
and has the LLM judge each changed table once; approve syncs the operator's
edits to Qdrant verbatim.

There is deliberately no server-side run state. The client holds the change
list between the two calls, which keeps the backend stateless and means a
restart mid-review costs nothing — detection is a diff, so re-scanning is
idempotent.

The LLM's role ends at the scan. It flags tables that look like junk and drafts
descriptions to start from; the operator then edits freely and whatever they
submit is what gets indexed. Nothing is re-judged on the way in.

The document-building itself lives in services/documenter.py, shared with the
CDC path (services/cdc.py) which does the same job unattended off a Debezium
event. Both must produce identical payloads for the same table, or a table
documented automatically and then edited by hand would show as permanently
changed in every subsequent diff.
"""

import logging

from fastapi import APIRouter, HTTPException

from app.models import (
    ApproveRequest,
    ApproveResponse,
    ColumnChangeModel,
    ReviewEntry,
    ReviewQueueResponse,
    ScanResponse,
    TableDocModel,
    TableChangeModel,
    VerdictModel,
)
from app.services import introspect, review_queue, reviewer
from app.services.catalog import (
    build_doc_text,
    delete_tables,
    ensure_collection,
    fetch_indexed_tables,
    upsert_tables,
)
from app.services.documenter import build_doc, build_packet
from app.services.embeddings import embed_texts
from app.services.reviewer import Verdict
from app.services.schema_diff import diff_schema

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.post("/scan", response_model=ScanResponse)
async def scan() -> ScanResponse:
    """Compare live MySQL against the indexed schema docs and review the drift.

    Returns one entry per changed table, each already prefilled with a proposed
    document and the reviewer's verdict. An unchanged database returns an empty
    list.
    """
    try:
        live = await introspect.introspect_schema()
    except Exception as exc:  # noqa: BLE001 — surfaced as a clean API error
        logger.exception("Schema introspection failed")
        raise HTTPException(status_code=503, detail=f"Could not read MySQL schema: {exc}") from exc

    try:
        indexed = await fetch_indexed_tables()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Reading indexed schema docs failed")
        raise HTTPException(status_code=503, detail=f"Could not read from Qdrant: {exc}") from exc

    changes = diff_schema(live, indexed)
    known_tables = set(live)

    reviewable = [change for change in changes if reviewer.needs_review(change)]
    packets = [await build_packet(change, indexed, known_tables) for change in reviewable]
    verdicts = await reviewer.review_many(packets)
    reviewed = dict(zip((c.table_name for c in reviewable), zip(packets, verdicts)))

    results = []
    for change in changes:
        if change.table_name in reviewed:
            packet, verdict = reviewed[change.table_name]
        else:
            # A dropped table is a mechanical delete: nothing to sample, nothing
            # to judge. It still needs approval, but not an LLM call.
            packet = await build_packet(change, indexed, known_tables)
            verdict = Verdict(needs_edit=False, severity=reviewer.SEVERITY_OK)

        results.append(
            TableChangeModel(
                change_type=change.change_type,
                table_name=change.table_name,
                summary=change.summary(),
                column_changes=[ColumnChangeModel(**vars(c)) for c in change.column_changes],
                doc=build_doc(change, packet, verdict),
                verdict=VerdictModel(**vars(verdict)),
                sample_rows=packet.sample_rows,
                row_count=packet.row_count,
            )
        )

    return ScanResponse(
        changes=results,
        indexed_table_count=len(indexed),
        live_table_count=len(live),
    )


@router.post("/approve", response_model=ApproveResponse)
async def approve(request: ApproveRequest) -> ApproveResponse:
    """Sync the reviewed changes to Qdrant exactly as submitted.

    The LLM reviews each changed table once, during the scan, to surface junk
    and to draft descriptions. After that the operator's text is authoritative:
    what is submitted here is what gets indexed, with no second opinion. A
    human who has just read the table and written its description knows more
    than a local 4B model does, and re-judging their text would make edits
    pointless — a table flagged during the scan could never be cleared no
    matter what was typed.
    """
    upserts = [item for item in request.items if item.action == "upsert"]
    deletes = [item for item in request.items if item.action == "delete"]
    skips = [item for item in request.items if item.action == "skip"]

    try:
        await ensure_collection()

        if upserts:
            docs = [item.doc.model_dump() for item in upserts]
            vectors = await embed_texts([build_doc_text(doc) for doc in docs])
            await upsert_tables(docs, vectors)

        await delete_tables([item.doc.table_name for item in deletes])
    except Exception as exc:  # noqa: BLE001
        logger.exception("Syncing approved changes to Qdrant failed")
        raise HTTPException(status_code=502, detail=f"Sync to Qdrant failed: {exc}") from exc

    # Anything just indexed or deleted has now been seen by a person, so it
    # leaves the review queue whether it arrived from a scan or from CDC. Skips
    # stay: skipping is "not now", and it should still be there next time.
    for item in upserts + deletes:
        review_queue.remove(item.doc.table_name)

    return ApproveResponse(
        upserted=[item.doc.table_name for item in upserts],
        deleted=[item.doc.table_name for item in deletes],
        skipped=[item.doc.table_name for item in skips],
    )


@router.get("/tables/{table_name}", response_model=TableDocModel)
async def get_table_doc(table_name: str) -> TableDocModel:
    """The document currently indexed for one table, for editing.

    Deliberately one table by name, not a catalog listing. It backs the "edit
    these descriptions" button on an activity event: you are already looking at
    what the model wrote and want to change it. Saving goes back through
    /approve, which overwrites the point verbatim.

    This is the only route to a description once its review-queue entry has been
    cleared — a scan will not surface the table, because a scan reports what
    DIFFERS from the index and this matches it.
    """
    try:
        indexed = await fetch_indexed_tables()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Reading indexed schema docs failed")
        raise HTTPException(status_code=503, detail=f"Could not read from Qdrant: {exc}") from exc

    payload = indexed.get(table_name)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"'{table_name}' is not indexed")

    # The stored payload also carries `level` and the derived `doc_text`;
    # pydantic drops both, and doc_text is regenerated on every upsert anyway.
    return TableDocModel(**payload)


@router.get("/review-queue", response_model=ReviewQueueResponse)
async def list_review_queue() -> ReviewQueueResponse:
    """Everything the automated path documented, flagged or not.

    Rendered by the same review card as a scan result. Two kinds arrive here:

    - `indexed: false` — flagged by the reviewer and NOT written to Qdrant. It
      cannot be queried until someone approves it.
    - `indexed: true`  — already in Qdrant and working. It is listed so the
      description the model wrote unattended can actually be read, and fixed if
      it is thin. A scan will never show it: a scan reports tables that differ
      from what is indexed, and this one matches.

    Editing either kind goes through /approve, which writes what it is given
    verbatim and clears the entry.
    """
    return ReviewQueueResponse(entries=[ReviewEntry(**entry) for entry in review_queue.listing()])


@router.delete("/review-queue/{table_name}")
async def dismiss_review(table_name: str) -> dict:
    """Drop an entry without changing what is indexed.

    Means "I have read this and I am happy" for an already-indexed table, and
    "the reviewer was right, this is junk" for a flagged one. Neither adds the
    table to any ignore list — it will come back if the table changes again.
    """
    removed = review_queue.remove(table_name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"'{table_name}' is not in the review queue")
    return {"dismissed": table_name}
