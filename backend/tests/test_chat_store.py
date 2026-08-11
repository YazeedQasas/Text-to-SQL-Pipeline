"""Tests for the chat store's pure logic and its isolation guard.

The SQL itself is not exercised here — that needs a real MySQL with
db/07_app_schema.sql applied, and there is no usable fake for a server whose
JSON columns and foreign keys are the behaviour under test. What IS tested is
everything that can go wrong without a database: the guard that keeps
transcripts out of the queryable schema, the row translation the replay path
depends on, and title generation.

The isolation guard gets the most attention of the three because it is the one
whose failure is silent. A store pointed at `legal_db` works perfectly — it just
also feeds every conversation to the retriever.
"""

import json

import pytest

from app.services import chat_store


# --- The isolation guard ------------------------------------------------------


def test_matching_database_is_refused(monkeypatch):
    """app_db must never be the schema the query pipeline reads."""
    monkeypatch.setattr(chat_store, "APP_MYSQL_DATABASE", "legal_db")
    monkeypatch.setattr(chat_store, "MYSQL_DATABASE", "legal_db")

    with pytest.raises(chat_store.ChatStoreError, match="same schema"):
        chat_store._assert_isolated()


def test_matching_user_is_refused(monkeypatch):
    """The account that runs model-generated SQL must not gain write access."""
    monkeypatch.setattr(chat_store, "APP_MYSQL_USER", "texttosql_ro")
    monkeypatch.setattr(chat_store, "MYSQL_USER", "texttosql_ro")

    with pytest.raises(chat_store.ChatStoreError, match="model-generated"):
        chat_store._assert_isolated()


def test_distinct_database_and_user_pass(monkeypatch):
    monkeypatch.setattr(chat_store, "APP_MYSQL_DATABASE", "app_db")
    monkeypatch.setattr(chat_store, "MYSQL_DATABASE", "legal_db")
    monkeypatch.setattr(chat_store, "APP_MYSQL_USER", "texttosql_app")
    monkeypatch.setattr(chat_store, "MYSQL_USER", "texttosql_ro")

    chat_store._assert_isolated()  # must not raise


def test_connect_refuses_before_opening_a_socket(monkeypatch):
    """The guard runs first, so a misconfigured store never connects at all."""
    monkeypatch.setattr(chat_store, "APP_MYSQL_DATABASE", "legal_db")
    monkeypatch.setattr(chat_store, "MYSQL_DATABASE", "legal_db")

    def fail(*args, **kwargs):
        raise AssertionError("pymysql.connect must not be reached")

    monkeypatch.setattr(chat_store.pymysql, "connect", fail)

    with pytest.raises(chat_store.ChatStoreError):
        chat_store._connect()


# --- Row translation ----------------------------------------------------------
# services/context.py and models.HistoryTurn both depend on these field names.


def _row(table_names, turn_id=1):
    from datetime import datetime

    return {
        "id": turn_id,
        "question": "كم عدد القضايا المفتوحة؟",
        "sql_text": "SELECT COUNT(*) FROM cases",
        "answer": "هناك 42 قضية مفتوحة.",
        "table_names": table_names,
        "created_at": datetime(2026, 8, 10, 12, 0, 0),
    }


def test_turn_maps_sql_text_onto_sql():
    """The column is renamed only to dodge a reserved word; HistoryTurn says `sql`."""
    turn = chat_store._row_to_turn(_row("[]"))

    assert turn["sql"] == "SELECT COUNT(*) FROM cases"
    assert "sql_text" not in turn


def test_table_names_parsed_from_json_string():
    turn = chat_store._row_to_turn(_row('["cases", "courts"]'))

    assert turn["table_names"] == ["cases", "courts"]


def test_table_names_accepted_already_parsed():
    """Driver and server versions disagree on whether JSON arrives parsed."""
    turn = chat_store._row_to_turn(_row(["cases", "courts"]))

    assert turn["table_names"] == ["cases", "courts"]


def test_unparseable_table_names_degrade_to_empty():
    """A corrupt column must cost the carry-over, not the whole conversation."""
    turn = chat_store._row_to_turn(_row("{not json"))

    assert turn["table_names"] == []


def test_non_list_table_names_degrade_to_empty():
    turn = chat_store._row_to_turn(_row('"cases"'))

    assert turn["table_names"] == []


def test_arabic_table_names_survive_a_round_trip():
    """ensure_ascii=False on write, so the column stays readable in a DB client."""
    encoded = json.dumps(["قضايا"], ensure_ascii=False)
    assert "قضايا" in encoded

    turn = chat_store._row_to_turn(_row(encoded))
    assert turn["table_names"] == ["قضايا"]


# --- Titles -------------------------------------------------------------------


def test_title_is_the_question_when_short():
    assert chat_store._title_from("كم عدد القضايا؟") == "كم عدد القضايا؟"


def test_title_collapses_whitespace():
    assert chat_store._title_from("  كم   عدد\nالقضايا؟ ") == "كم عدد القضايا؟"


def test_long_title_is_truncated_within_the_column():
    title = chat_store._title_from("ق" * 500)

    assert len(title) <= chat_store.TITLE_MAX_CHARS
    assert title.endswith("…")


def test_truncation_leaves_room_for_the_ellipsis():
    """The ellipsis must fit inside the cap, not push the title past it."""
    for length in (chat_store.TITLE_MAX_CHARS - 1, chat_store.TITLE_MAX_CHARS, chat_store.TITLE_MAX_CHARS + 1):
        assert len(chat_store._title_from("x" * length)) <= chat_store.TITLE_MAX_CHARS


def test_title_cap_fits_the_column_width():
    """VARCHAR(255) in db/07_app_schema.sql. Keep these in step."""
    assert chat_store.TITLE_MAX_CHARS <= 255


# --- Timestamps ---------------------------------------------------------------


def test_epoch_of_missing_datetime_is_zero():
    assert chat_store._epoch(None) == 0.0


def test_stored_datetime_is_read_back_as_utc():
    """Stored naive-UTC, so the epoch must not shift with the server's zone."""
    from datetime import datetime, timezone

    stored = datetime(2026, 8, 10, 12, 0, 0)
    expected = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc).timestamp()

    assert chat_store._epoch(stored) == expected


def test_history_limit_of_zero_reads_nothing(monkeypatch):
    """A zero cap must short-circuit rather than issue LIMIT 0."""
    import asyncio

    def fail(*args, **kwargs):
        raise AssertionError("no query should be issued for a zero limit")

    monkeypatch.setattr(chat_store, "_load_history_sync", fail)

    assert asyncio.run(chat_store.load_history("some-chat-id", limit=0)) == []
