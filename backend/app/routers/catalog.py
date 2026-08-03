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
"""

import logging

from fastapi import APIRouter, HTTPException

from app.config import CATALOG_SAMPLE_ROWS
from app.models import (
    ApproveRequest,
    ApproveResponse,
    ColumnChangeModel,
    ScanResponse,
    TableChangeModel,
    TableDocModel,
    VerdictModel,
)
from app.services import introspect, reviewer
from app.services.catalog import (
    build_doc_text,
    delete_tables,
    ensure_collection,
    fetch_indexed_tables,
    upsert_tables,
)
from app.services.embeddings import embed_texts
from app.services.introspect import LiveTable
from app.services.reviewer import ReviewPacket, Verdict
from app.services.schema_diff import TableChange, diff_schema

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


def _domain_context(indexed: dict[str, dict], exclude: str) -> list[dict]:
    """Descriptions of everything already indexed, minus the table being judged.

    The table under review is excluded so the model judges it against the rest
    of the catalog rather than against its own (possibly wrong) description.
    """
    return [
        {"table_name": name, "description": payload.get("description", "")}
        for name, payload in sorted(indexed.items())
        if name != exclude
    ]


def _live_columns_as_dicts(live: LiveTable) -> list[dict]:
    return [
        {"name": c.name, "type": c.type, "nullable": c.nullable, "comment": c.comment}
        for c in live.columns
    ]


async def _sample(table_name: str, known_tables: set[str]) -> tuple[list[dict], int]:
    if table_name not in known_tables:
        return [], -1
    rows = await introspect.sample_rows(table_name, known_tables, CATALOG_SAMPLE_ROWS)
    count = await introspect.row_count(table_name, known_tables)
    return rows, count


async def _build_packet(
    change: TableChange, indexed: dict[str, dict], known_tables: set[str]
) -> ReviewPacket:
    indexed_doc = change.indexed or {}
    indexed_columns = {c["name"]: c for c in indexed_doc.get("columns", [])}

    if change.live is not None:
        columns = [
            {**column, "description": indexed_columns.get(column["name"], {}).get("description", "")}
            for column in _live_columns_as_dicts(change.live)
        ]
        primary_key = change.live.primary_key
        foreign_keys = [
            {
                "column": fk.column,
                "references_table": fk.references_table,
                "references_column": fk.references_column,
            }
            for fk in change.live.foreign_keys
        ]
    else:
        columns = indexed_doc.get("columns", [])
        primary_key = indexed_doc.get("primary_key", "")
        foreign_keys = indexed_doc.get("foreign_keys", [])

    sample_rows, row_count = await _sample(change.table_name, known_tables)

    return ReviewPacket(
        table_name=change.table_name,
        change_summary=change.summary(),
        columns=columns,
        primary_key=primary_key,
        foreign_keys=foreign_keys,
        sample_rows=sample_rows,
        row_count=row_count,
        current_description=indexed_doc.get("description", ""),
        domain_context=_domain_context(indexed, change.table_name),
    )


def _build_doc(change: TableChange, packet: ReviewPacket, verdict: Verdict) -> TableDocModel:
    """Prefill the document the user will edit and eventually approve.

    Descriptions resolve in order: what is already indexed for that column,
    then the reviewer's suggestion, then the MySQL COLUMN_COMMENT. The table
    description keeps whatever is already indexed — the reviewer's suggestion
    travels separately on the verdict so the UI can offer it without silently
    overwriting curated text.
    """
    columns = []
    for column in packet.columns:
        description = (
            column.get("description")
            or verdict.suggested_column_descriptions.get(column["name"], "")
            or column.get("comment", "")
        )
        columns.append(
            {
                "name": column["name"],
                "type": column["type"],
                "nullable": bool(column.get("nullable")),
                "description": description.strip(),
            }
        )

    return TableDocModel(
        table_name=change.table_name,
        description=(packet.current_description or verdict.suggested_description).strip(),
        primary_key=packet.primary_key,
        columns=columns,
        foreign_keys=packet.foreign_keys,
    )


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
    packets = [await _build_packet(change, indexed, known_tables) for change in reviewable]
    verdicts = await reviewer.review_many(packets)
    reviewed = dict(zip((c.table_name for c in reviewable), zip(packets, verdicts)))

    results = []
    for change in changes:
        if change.table_name in reviewed:
            packet, verdict = reviewed[change.table_name]
        else:
            # A dropped table is a mechanical delete: nothing to sample, nothing
            # to judge. It still needs approval, but not an LLM call.
            packet = await _build_packet(change, indexed, known_tables)
            verdict = Verdict(needs_edit=False, severity=reviewer.SEVERITY_OK)

        results.append(
            TableChangeModel(
                change_type=change.change_type,
                table_name=change.table_name,
                summary=change.summary(),
                column_changes=[ColumnChangeModel(**vars(c)) for c in change.column_changes],
                doc=_build_doc(change, packet, verdict),
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

    return ApproveResponse(
        upserted=[item.doc.table_name for item in upserts],
        deleted=[item.doc.table_name for item in deletes],
        skipped=[item.doc.table_name for item in skips],
    )
