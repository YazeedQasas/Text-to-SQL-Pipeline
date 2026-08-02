import pytest

from app.services.sql_guard import SQLGuardError, enforce_select_only


def test_allows_plain_select():
    sql = enforce_select_only("SELECT * FROM cases")
    assert sql.upper().startswith("SELECT")
    assert "LIMIT" in sql.upper()


def test_preserves_existing_limit():
    sql = enforce_select_only("SELECT * FROM cases LIMIT 5")
    assert sql.count("LIMIT") == 1 or sql.upper().count("LIMIT") == 1


def test_rejects_insert():
    with pytest.raises(SQLGuardError):
        enforce_select_only("INSERT INTO cases (title) VALUES ('x')")


def test_rejects_update():
    with pytest.raises(SQLGuardError):
        enforce_select_only("UPDATE cases SET status='Closed'")


def test_rejects_delete():
    with pytest.raises(SQLGuardError):
        enforce_select_only("DELETE FROM cases")


def test_rejects_drop():
    with pytest.raises(SQLGuardError):
        enforce_select_only("DROP TABLE cases")


def test_rejects_multiple_statements():
    with pytest.raises(SQLGuardError):
        enforce_select_only("SELECT * FROM cases; DROP TABLE cases;")


def test_rejects_stacked_query_via_semicolon_injection():
    with pytest.raises(SQLGuardError):
        enforce_select_only("SELECT * FROM cases WHERE 1=1; DELETE FROM cases WHERE 1=1")


def test_rejects_comments():
    with pytest.raises(SQLGuardError):
        enforce_select_only("SELECT * FROM cases -- DROP TABLE cases")


def test_rejects_empty_sql():
    with pytest.raises(SQLGuardError):
        enforce_select_only("")


def test_allows_joins():
    sql = enforce_select_only(
        "SELECT c.title, j.verdict FROM cases c JOIN judgements j ON c.case_id = j.case_id"
    )
    assert "JOIN" in sql.upper()
