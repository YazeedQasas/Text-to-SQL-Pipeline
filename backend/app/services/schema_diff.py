"""Structural diff between live MySQL and what is currently indexed in Qdrant.

The indexed Qdrant payloads carry the full column list, primary key and
foreign keys as of the last successful ingestion, which makes them a usable
"last known state" record — no separate migration log or timestamp tracking is
needed. `information_schema.TABLES.UPDATE_TIME` is deliberately not used: it is
NULL or stale on InnoDB and says nothing about DDL.

Pure functions only, no I/O, so this is the layer that is cheap to test.
"""

from dataclasses import dataclass, field

from app.services.introspect import LiveTable, normalize_type

TABLE_ADDED = "table_added"
TABLE_DROPPED = "table_dropped"
TABLE_MODIFIED = "table_modified"

COLUMN_ADDED = "column_added"
COLUMN_DROPPED = "column_dropped"
COLUMN_TYPE_CHANGED = "column_type_changed"
COLUMN_NULLABILITY_CHANGED = "column_nullability_changed"


@dataclass
class ColumnChange:
    change_type: str
    column: str
    before: str | None = None
    after: str | None = None


@dataclass
class TableChange:
    """One reviewable unit of drift.

    A table with five new columns is a single TableChange carrying five
    ColumnChanges, because review and approval both happen per table — the
    reviewer judges one description against one table, and the user approves
    one card.
    """

    change_type: str
    table_name: str
    live: LiveTable | None = None
    indexed: dict | None = None
    column_changes: list[ColumnChange] = field(default_factory=list)

    def summary(self) -> str:
        """Short human/LLM-readable description of what changed."""
        if self.change_type == TABLE_ADDED:
            return f"New table '{self.table_name}' exists in MySQL but is not indexed."
        if self.change_type == TABLE_DROPPED:
            return f"Table '{self.table_name}' is indexed but no longer exists in MySQL."

        parts = []
        for change in self.column_changes:
            if change.change_type == COLUMN_ADDED:
                parts.append(f"new column {change.column} ({change.after})")
            elif change.change_type == COLUMN_DROPPED:
                parts.append(f"dropped column {change.column} (was {change.before})")
            elif change.change_type == COLUMN_TYPE_CHANGED:
                parts.append(f"{change.column} type changed {change.before} -> {change.after}")
            elif change.change_type == COLUMN_NULLABILITY_CHANGED:
                parts.append(f"{change.column} nullability changed {change.before} -> {change.after}")
        return f"Table '{self.table_name}' changed: " + "; ".join(parts)


def _indexed_columns_by_name(indexed: dict) -> dict[str, dict]:
    return {column["name"]: column for column in indexed.get("columns", [])}


def _nullability_label(nullable: bool) -> str:
    return "NULL" if nullable else "NOT NULL"


def diff_columns(live: LiveTable, indexed: dict) -> list[ColumnChange]:
    """Compare one table's live columns against its indexed payload columns."""
    indexed_columns = _indexed_columns_by_name(indexed)
    live_columns = {column.name: column for column in live.columns}

    changes: list[ColumnChange] = []

    for column in live.columns:
        existing = indexed_columns.get(column.name)
        if existing is None:
            changes.append(
                ColumnChange(change_type=COLUMN_ADDED, column=column.name, after=column.type)
            )
            continue

        if normalize_type(existing["type"]) != normalize_type(column.type):
            changes.append(
                ColumnChange(
                    change_type=COLUMN_TYPE_CHANGED,
                    column=column.name,
                    before=existing["type"],
                    after=column.type,
                )
            )

        if bool(existing.get("nullable")) != column.nullable:
            changes.append(
                ColumnChange(
                    change_type=COLUMN_NULLABILITY_CHANGED,
                    column=column.name,
                    before=_nullability_label(bool(existing.get("nullable"))),
                    after=_nullability_label(column.nullable),
                )
            )

    for name, existing in indexed_columns.items():
        if name not in live_columns:
            changes.append(
                ColumnChange(
                    change_type=COLUMN_DROPPED, column=name, before=existing.get("type")
                )
            )

    return changes


def diff_schema(live: dict[str, LiveTable], indexed: dict[str, dict]) -> list[TableChange]:
    """Return one TableChange per table that differs. Unchanged tables are omitted."""
    changes: list[TableChange] = []

    for table_name in sorted(live):
        live_table = live[table_name]
        indexed_table = indexed.get(table_name)

        if indexed_table is None:
            changes.append(
                TableChange(change_type=TABLE_ADDED, table_name=table_name, live=live_table)
            )
            continue

        column_changes = diff_columns(live_table, indexed_table)
        if column_changes:
            changes.append(
                TableChange(
                    change_type=TABLE_MODIFIED,
                    table_name=table_name,
                    live=live_table,
                    indexed=indexed_table,
                    column_changes=column_changes,
                )
            )

    for table_name in sorted(indexed):
        if table_name not in live:
            changes.append(
                TableChange(
                    change_type=TABLE_DROPPED,
                    table_name=table_name,
                    indexed=indexed[table_name],
                )
            )

    return changes
