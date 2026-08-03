"""Tests for the structural diff between live MySQL and indexed Qdrant payloads.

The most important case here is type normalization: the indexed docs were
hand-written in uppercase (`VARCHAR(255)`) while `information_schema` reports
lowercase (`varchar(255)`). If that regresses, the very first scan reports
every table in the database as modified and the feature is unusable.
"""

from app.services.introspect import LiveColumn, LiveTable, normalize_type
from app.services.schema_diff import (
    COLUMN_ADDED,
    COLUMN_DROPPED,
    COLUMN_NULLABILITY_CHANGED,
    COLUMN_TYPE_CHANGED,
    TABLE_ADDED,
    TABLE_DROPPED,
    TABLE_MODIFIED,
    diff_schema,
)


def _live_judges() -> LiveTable:
    """The `judges` table exactly as information_schema reports it."""
    return LiveTable(
        name="judges",
        primary_key="judge_id",
        columns=[
            LiveColumn("judge_id", "int", False),
            LiveColumn("full_name", "varchar(255)", False),
            LiveColumn("title", "varchar(100)", False),
            LiveColumn("court_id", "int", False),
            LiveColumn("appointed_date", "date", True),
        ],
    )


def _indexed_judges() -> dict:
    """The `judges` payload exactly as it is stored in Qdrant today."""
    return {
        "table_name": "judges",
        "description": "Judicial officers who preside over hearings.",
        "primary_key": "judge_id",
        "columns": [
            {"name": "judge_id", "type": "INT", "nullable": False, "description": "PK."},
            {"name": "full_name", "type": "VARCHAR(255)", "nullable": False, "description": "Name."},
            {"name": "title", "type": "VARCHAR(100)", "nullable": False, "description": "Title."},
            {"name": "court_id", "type": "INT", "nullable": False, "description": "FK."},
            {"name": "appointed_date", "type": "DATE", "nullable": True, "description": "Date."},
        ],
        "foreign_keys": [],
    }


# --- normalization ------------------------------------------------------------


def test_normalize_type_ignores_case():
    assert normalize_type("VARCHAR(255)") == normalize_type("varchar(255)")


def test_normalize_type_ignores_integer_display_width():
    assert normalize_type("INT") == normalize_type("int(11)")


def test_normalize_type_ignores_spacing_in_enums():
    assert normalize_type("ENUM('Open', 'Closed')") == normalize_type("enum('Open','Closed')")


def test_normalize_type_still_distinguishes_real_differences():
    assert normalize_type("VARCHAR(255)") != normalize_type("varchar(100)")
    assert normalize_type("INT") != normalize_type("bigint")


# --- unchanged schema ---------------------------------------------------------


def test_identical_schema_produces_no_changes():
    changes = diff_schema({"judges": _live_judges()}, {"judges": _indexed_judges()})
    assert changes == []


def test_case_difference_alone_is_not_a_change():
    """The regression that would flag every table on the first scan."""
    live = {"judges": _live_judges()}
    indexed = {"judges": _indexed_judges()}
    for column in indexed["judges"]["columns"]:
        column["type"] = column["type"].upper()

    assert diff_schema(live, indexed) == []


# --- table-level changes ------------------------------------------------------


def test_new_table_is_reported_as_added():
    live = {
        "judges": _live_judges(),
        "aa_aa_a": LiveTable(name="aa_aa_a", columns=[LiveColumn("x", "text", True)]),
    }
    changes = diff_schema(live, {"judges": _indexed_judges()})

    assert len(changes) == 1
    assert changes[0].change_type == TABLE_ADDED
    assert changes[0].table_name == "aa_aa_a"
    assert changes[0].live is not None
    assert changes[0].indexed is None


def test_removed_table_is_reported_as_dropped():
    changes = diff_schema({}, {"judges": _indexed_judges()})

    assert len(changes) == 1
    assert changes[0].change_type == TABLE_DROPPED
    assert changes[0].table_name == "judges"
    assert changes[0].live is None
    assert changes[0].indexed is not None


# --- column-level changes -----------------------------------------------------


def test_added_column_is_detected():
    live_table = _live_judges()
    live_table.columns.append(LiveColumn("retired_date", "date", True))

    changes = diff_schema({"judges": live_table}, {"judges": _indexed_judges()})

    assert len(changes) == 1
    assert changes[0].change_type == TABLE_MODIFIED
    assert [(c.change_type, c.column) for c in changes[0].column_changes] == [
        (COLUMN_ADDED, "retired_date")
    ]


def test_dropped_column_is_detected():
    live_table = _live_judges()
    live_table.columns = [c for c in live_table.columns if c.name != "title"]

    changes = diff_schema({"judges": live_table}, {"judges": _indexed_judges()})

    assert [(c.change_type, c.column) for c in changes[0].column_changes] == [
        (COLUMN_DROPPED, "title")
    ]


def test_type_change_is_detected():
    live_table = _live_judges()
    live_table.columns[1] = LiveColumn("full_name", "varchar(500)", False)

    changes = diff_schema({"judges": live_table}, {"judges": _indexed_judges()})
    column_change = changes[0].column_changes[0]

    assert column_change.change_type == COLUMN_TYPE_CHANGED
    assert column_change.column == "full_name"
    assert column_change.before == "VARCHAR(255)"
    assert column_change.after == "varchar(500)"


def test_nullability_change_is_detected():
    live_table = _live_judges()
    live_table.columns[4] = LiveColumn("appointed_date", "date", False)

    changes = diff_schema({"judges": live_table}, {"judges": _indexed_judges()})
    column_change = changes[0].column_changes[0]

    assert column_change.change_type == COLUMN_NULLABILITY_CHANGED
    assert column_change.before == "NULL"
    assert column_change.after == "NOT NULL"


def test_multiple_column_changes_collapse_into_one_table_change():
    live_table = _live_judges()
    live_table.columns = [c for c in live_table.columns if c.name != "title"]
    live_table.columns.append(LiveColumn("retired_date", "date", True))
    live_table.columns.append(LiveColumn("chamber", "varchar(50)", True))

    changes = diff_schema({"judges": live_table}, {"judges": _indexed_judges()})

    assert len(changes) == 1, "review and approval are per table, not per column"
    assert len(changes[0].column_changes) == 3


def test_summary_mentions_every_column_change():
    live_table = _live_judges()
    live_table.columns.append(LiveColumn("retired_date", "date", True))

    summary = diff_schema({"judges": live_table}, {"judges": _indexed_judges()})[0].summary()

    assert "judges" in summary
    assert "retired_date" in summary
