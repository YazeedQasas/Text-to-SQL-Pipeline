"""Live MySQL schema introspection for the catalog review flow.

Reads the current structure of the configured database out of
`information_schema` so it can be diffed against what is currently indexed in
Qdrant (see schema_diff.py). Also pulls a small sample of real rows per table,
which is what lets the LLM reviewer judge whether a newly-appeared table
actually holds domain data or is somebody's scratch import.

Everything here goes through the same read-only connection as the query
pipeline — introspection needs no extra grants, because MySQL exposes
`information_schema` rows for any object the user holds a privilege on, and
`texttosql_ro` holds SELECT on the whole schema.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from app.config import MYSQL_DATABASE
from app.services.db import execute_select
from app.services.sql_guard import enforce_select_only

logger = logging.getLogger(__name__)

# Display widths on integer types are cosmetic and were dropped entirely in
# MySQL 8.0.19, so `int(11)` and `int` describe the same column. Normalizing
# them away stops a server-version difference from looking like schema drift.
_INT_DISPLAY_WIDTH_RE = re.compile(r"\b(tinyint|smallint|mediumint|int|integer|bigint)\(\d+\)")
_SPACE_AFTER_COMMA_RE = re.compile(r",\s+")


@dataclass
class LiveColumn:
    name: str
    type: str
    nullable: bool
    comment: str = ""


@dataclass
class LiveForeignKey:
    column: str
    references_table: str
    references_column: str


@dataclass
class LiveTable:
    name: str
    columns: list[LiveColumn] = field(default_factory=list)
    primary_key: str = ""
    foreign_keys: list[LiveForeignKey] = field(default_factory=list)


def normalize_type(sql_type: str) -> str:
    """Canonicalize a SQL type string so both sides of a diff compare equal.

    The indexed docs were hand-written as `VARCHAR(255)` / `ENUM('a','b')`
    while `information_schema.COLUMN_TYPE` reports `varchar(255)` /
    `enum('a','b')`. Without this every table would show up as changed on the
    very first scan.
    """
    normalized = " ".join(sql_type.strip().lower().split())
    normalized = _SPACE_AFTER_COMMA_RE.sub(",", normalized)
    return _INT_DISPLAY_WIDTH_RE.sub(r"\1", normalized)


def quote_identifier(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


async def introspect_schema() -> dict[str, LiveTable]:
    """Return the live structure of every base table and view, keyed by name.

    Views are included because they are queryable and therefore worth putting in
    front of the LLM — `case_congestion` computes judicial congestion at read
    time, which a generated column cannot do (CURDATE() is non-deterministic and
    MySQL rejects it in a generated column). A view simply has no primary key
    and no foreign keys, which the rest of the pipeline already tolerates.
    """
    _, table_rows = await execute_select(
        """
        SELECT TABLE_NAME
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE', 'VIEW')
        ORDER BY TABLE_NAME
        """,
        (MYSQL_DATABASE,),
    )
    tables: dict[str, LiveTable] = {
        row["TABLE_NAME"]: LiveTable(name=row["TABLE_NAME"]) for row in table_rows
    }

    _, column_rows = await execute_select(
        """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_COMMENT
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME, ORDINAL_POSITION
        """,
        (MYSQL_DATABASE,),
    )
    for row in column_rows:
        table = tables.get(row["TABLE_NAME"])
        if table is None:
            continue  # a view, or a table dropped between the two queries
        table.columns.append(
            LiveColumn(
                name=row["COLUMN_NAME"],
                type=row["COLUMN_TYPE"],
                nullable=row["IS_NULLABLE"] == "YES",
                comment=row["COLUMN_COMMENT"] or "",
            )
        )

    # Primary keys and foreign keys both live in KEY_COLUMN_USAGE; the PRIMARY
    # constraint name is reserved by MySQL, so one pass covers both.
    _, key_rows = await execute_select(
        """
        SELECT TABLE_NAME, COLUMN_NAME, CONSTRAINT_NAME,
               REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
        FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s
          AND (CONSTRAINT_NAME = 'PRIMARY' OR REFERENCED_TABLE_NAME IS NOT NULL)
        ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION
        """,
        (MYSQL_DATABASE,),
    )
    primary_key_columns: dict[str, list[str]] = {}
    for row in key_rows:
        table = tables.get(row["TABLE_NAME"])
        if table is None:
            continue
        if row["CONSTRAINT_NAME"] == "PRIMARY":
            primary_key_columns.setdefault(row["TABLE_NAME"], []).append(row["COLUMN_NAME"])
        if row["REFERENCED_TABLE_NAME"]:
            table.foreign_keys.append(
                LiveForeignKey(
                    column=row["COLUMN_NAME"],
                    references_table=row["REFERENCED_TABLE_NAME"],
                    references_column=row["REFERENCED_COLUMN_NAME"],
                )
            )

    for table_name, pk_columns in primary_key_columns.items():
        # Composite keys are rendered as one comma-joined string to match the
        # single `primary_key` field the indexed payloads already use.
        tables[table_name].primary_key = ", ".join(pk_columns)

    return tables


async def sample_rows(table_name: str, known_tables: set[str], limit: int) -> list[dict]:
    """Fetch up to `limit` real rows from a table, for LLM review.

    The table name is interpolated rather than parameterized (SQL forbids
    parameters in a FROM clause), so it is checked against the set of tables
    introspection actually returned, backtick-quoted, and then run through the
    same read-only guard as generated SQL.

    Returns [] rather than raising if the sample can't be taken — a reviewer
    with no sample data is more likely to flag the table for a human, which is
    the direction we want to fail in.
    """
    if table_name not in known_tables:
        raise ValueError(f"Refusing to sample unknown table: {table_name!r}")

    sql = f"SELECT * FROM {quote_identifier(table_name)} LIMIT {int(limit)}"
    try:
        _, rows = await execute_select(enforce_select_only(sql))
    except Exception as exc:  # noqa: BLE001 — sampling is best-effort
        logger.warning("Could not sample rows from %s: %s", table_name, exc)
        return []
    return [json_safe_row(row) for row in rows]


def json_safe(value: object) -> object:
    """Convert one MySQL cell into something json.dumps can write.

    The driver returns real Python objects for several common column types, and
    none of them are JSON-serializable: DECIMAL becomes `Decimal`, DATE and
    DATETIME become `date`/`datetime`, TIME becomes `timedelta`, and binary
    columns become `bytes`. Sample rows travel from here into the review queue
    file, the activity log and the SSE stream, so one unconverted cell breaks
    the whole documentation run for that table.

    Found the hard way: a table with `amount DECIMAL(10,2)` and `paid_date DATE`
    failed with "Object of type Decimal is not JSON serializable", after an
    earlier test table of only INT/TEXT columns passed cleanly.

    DECIMAL becomes a STRING rather than a float on purpose. These are money
    values; float would silently round them, and the reviewer only ever reads
    them as text anyway.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        # Binary columns are not worth showing the reviewer, but their presence
        # is: a length reads better than mojibake from a bad decode.
        return f"<{len(bytes(value))} bytes>"
    return value


def json_safe_row(row: dict) -> dict:
    return {key: json_safe(value) for key, value in row.items()}


async def row_count(table_name: str, known_tables: set[str]) -> int:
    """Total row count for a table, or -1 if it could not be determined.

    An empty brand-new table is itself a signal to the reviewer, so this is
    worth reporting even though it is not part of the structural diff.
    """
    if table_name not in known_tables:
        raise ValueError(f"Refusing to count unknown table: {table_name!r}")

    sql = f"SELECT COUNT(*) AS n FROM {quote_identifier(table_name)}"
    try:
        _, rows = await execute_select(enforce_select_only(sql))
    except Exception as exc:  # noqa: BLE001 — best-effort, same rationale as sample_rows
        logger.warning("Could not count rows in %s: %s", table_name, exc)
        return -1
    return int(rows[0]["n"]) if rows else 0
