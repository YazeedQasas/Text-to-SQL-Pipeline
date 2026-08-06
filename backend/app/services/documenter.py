"""Turn a detected schema change into a reviewed, indexable table document.

Extracted from routers/catalog.py so the two paths that document a table share
one implementation:

- the manual flow, where an operator clicks Scan, edits the drafts and approves;
- the CDC flow (services/cdc.py), where a Debezium event does the same thing
  unattended and the result waits in a review queue instead of on a screen.

Keeping these together matters because they must agree on what a document looks
like. If the CDC path built its payloads even slightly differently, a table
documented automatically and the same table documented by hand would produce two
different points, and the diff would show the table as permanently changed.
"""

import logging

from app.config import CATALOG_SAMPLE_ROWS
from app.models import TableDocModel
from app.services import introspect
from app.services.introspect import LiveTable
from app.services.reviewer import ReviewPacket, Verdict
from app.services.schema_diff import TableChange

logger = logging.getLogger(__name__)


def domain_context(indexed: dict[str, dict], exclude: str) -> list[dict]:
    """Descriptions of everything already indexed, minus the table being judged.

    The table under review is excluded so the model judges it against the rest
    of the catalog rather than against its own (possibly wrong) description.
    """
    return [
        {"table_name": name, "description": payload.get("description", "")}
        for name, payload in sorted(indexed.items())
        if name != exclude
    ]


def live_columns_as_dicts(live: LiveTable) -> list[dict]:
    return [
        {"name": c.name, "type": c.type, "nullable": c.nullable, "comment": c.comment}
        for c in live.columns
    ]


async def sample(table_name: str, known_tables: set[str]) -> tuple[list[dict], int]:
    if table_name not in known_tables:
        return [], -1
    rows = await introspect.sample_rows(table_name, known_tables, CATALOG_SAMPLE_ROWS)
    count = await introspect.row_count(table_name, known_tables)
    return rows, count


async def build_packet(
    change: TableChange, indexed: dict[str, dict], known_tables: set[str]
) -> ReviewPacket:
    indexed_doc = change.indexed or {}
    indexed_columns = {c["name"]: c for c in indexed_doc.get("columns", [])}

    if change.live is not None:
        columns = [
            {**column, "description": indexed_columns.get(column["name"], {}).get("description", "")}
            for column in live_columns_as_dicts(change.live)
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

    sample_rows, row_count = await sample(change.table_name, known_tables)

    return ReviewPacket(
        table_name=change.table_name,
        change_summary=change.summary(),
        columns=columns,
        primary_key=primary_key,
        foreign_keys=foreign_keys,
        sample_rows=sample_rows,
        row_count=row_count,
        current_description=indexed_doc.get("description", ""),
        domain_context=domain_context(indexed, change.table_name),
    )


def build_doc(change: TableChange, packet: ReviewPacket, verdict: Verdict) -> TableDocModel:
    """Prefill the document that will be edited and eventually indexed.

    Descriptions resolve in order: what is already indexed for that column,
    then the reviewer's suggestion, then the MySQL COLUMN_COMMENT. The table
    description keeps whatever is already indexed — the reviewer's suggestion
    travels separately on the verdict so the UI can offer it without silently
    overwriting curated text.

    The CDC path relies on the same precedence: a brand-new table has nothing
    indexed, so the reviewer's suggestion is what gets written. An existing
    table's curated description survives a column being added to it.
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
