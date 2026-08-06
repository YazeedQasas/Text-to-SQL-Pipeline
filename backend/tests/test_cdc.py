"""Tests for the Debezium intake: event parsing and the debounce window.

Two things are worth pinning down here.

The parser has to survive Debezium's output format changing under it. Which
envelope arrives depends on properties set in debezium/conf/application.properties,
which is easy to edit without anyone remembering this code reads it — so all
three shapes are tested, not just the configured one.

The debounce is the part that would otherwise be silently wrong. A CREATE TABLE
arrives when the table is empty, and sample rows are the reviewer's main
evidence; firing immediately means every new table gets judged with nothing to
look at.
"""

from app.services.cdc import (
    SIGNAL_DDL,
    SIGNAL_ROW,
    CdcBuffer,
    PendingTable,
    TableSignal,
    parse_body,
    parse_event,
)
from app.services.qdrant_watch import (
    DELETE_COLLECTION,
    DELETE_POINTS,
    parse_delete_signal,
)

# Captured from the shape Debezium's MySQL connector emits for DDL.
SCHEMA_CHANGE_EVENT = {
    "source": {"db": "legal_db", "table": "case_notes"},
    "databaseName": "legal_db",
    "ddl": "CREATE TABLE `case_notes` (`id` INT NOT NULL, `body` TEXT)",
    "tableChanges": [
        {
            "type": "CREATE",
            "id": '"legal_db"."case_notes"',
            "table": {"primaryKeyColumnNames": ["id"], "columns": []},
        }
    ],
}

ROW_EVENT = {
    "before": None,
    "after": {"id": 1, "body": "ملاحظة"},
    "source": {"db": "legal_db", "table": "case_notes", "ts_ms": 1_700_000_000},
    "op": "c",
}


# --- Event parsing ------------------------------------------------------------


def test_a_schema_change_event_names_its_table():
    signals = parse_event(SCHEMA_CHANGE_EVENT)

    assert [(s.table_name, s.kind) for s in signals] == [("case_notes", SIGNAL_DDL)]
    assert "CREATE TABLE" in signals[0].ddl


def test_a_row_event_names_its_table():
    signals = parse_event(ROW_EVENT)

    assert [(s.table_name, s.kind) for s in signals] == [("case_notes", SIGNAL_ROW)]


def test_the_schemas_enabled_envelope_is_unwrapped():
    wrapped = {"schema": {"type": "struct", "fields": []}, "payload": ROW_EVENT}

    assert [s.table_name for s in parse_event(wrapped)] == ["case_notes"]


def test_the_cloudevents_envelope_is_unwrapped():
    wrapped = {"id": "abc", "type": "io.debezium", "data": ROW_EVENT}

    assert [s.table_name for s in parse_event(wrapped)] == ["case_notes"]


def test_events_from_another_database_are_ignored():
    """Other schemas on the same MySQL server come down the same sink."""
    other = {**ROW_EVENT, "source": {"db": "wordpress", "table": "wp_posts"}}

    assert parse_event(other) == []


def test_heartbeats_and_junk_are_ignored_rather_than_erroring():
    """Answering these with an error would make the connector retry forever."""
    assert parse_event({}) == []
    assert parse_event({"ts_ms": 1_700_000_000}) == []
    assert parse_body(None) == []
    assert parse_body("not an event") == []


def test_a_batch_of_events_is_flattened():
    signals = parse_body([SCHEMA_CHANGE_EVENT, ROW_EVENT])

    assert len(signals) == 2
    assert {s.kind for s in signals} == {SIGNAL_DDL, SIGNAL_ROW}


def test_a_multi_table_ddl_yields_one_signal_per_table():
    event = {
        "databaseName": "legal_db",
        "ddl": "…",
        "tableChanges": [
            {"type": "CREATE", "id": '"legal_db"."a"'},
            {"type": "ALTER", "id": '"legal_db"."b"'},
        ],
    }

    assert [s.table_name for s in parse_event(event)] == ["a", "b"]


def test_backtick_quoted_identifiers_are_handled():
    """MySQL's quoting varies with the server's sql_mode."""
    event = {"tableChanges": [{"type": "CREATE", "id": "`legal_db`.`cases`"}]}

    assert [s.table_name for s in parse_event(event)] == ["cases"]


# --- The debounce window ------------------------------------------------------


def test_a_table_is_not_documented_the_instant_it_is_created():
    """The whole reason the debounce exists: a fresh table has no rows yet."""
    entry = PendingTable(table_name="case_notes", first_seen=1000.0, last_event=1000.0)

    assert entry.is_due(now=1000.5) is False


def test_a_table_fires_once_its_writes_go_quiet():
    entry = PendingTable(table_name="case_notes", first_seen=1000.0, last_event=1000.0)

    # CDC_DEBOUNCE_SECONDS defaults to 20.
    assert entry.is_due(now=1021.0) is True


def test_continuing_writes_hold_the_table_back():
    """A bulk load should produce one review after it finishes, not one per batch."""
    buffer = CdcBuffer()
    buffer.add(TableSignal("case_notes", SIGNAL_DDL), now=1000.0)

    for tick in range(1, 15):
        buffer.add(TableSignal("case_notes", SIGNAL_ROW), now=1000.0 + tick * 5)
        assert buffer.take_due(now=1000.0 + tick * 5) == []

    # 70s of writes; the ceiling is 120s, so it is the quiet period that fires it.
    assert [e.table_name for e in buffer.take_due(now=1091.0)] == ["case_notes"]


def test_a_never_ending_load_still_gets_documented_eventually():
    """Otherwise a continuously-written table would never be documented at all."""
    entry = PendingTable(table_name="events", first_seen=1000.0, last_event=1119.0)

    # CDC_MAX_WAIT_SECONDS defaults to 120: quiet for only 2s, but waited 121.
    assert entry.is_due(now=1121.0) is True


def test_taking_due_tables_removes_them_from_the_buffer():
    buffer = CdcBuffer()
    buffer.add(TableSignal("a", SIGNAL_DDL), now=1000.0)

    assert len(buffer.take_due(now=1100.0)) == 1
    assert buffer.take_due(now=1200.0) == []
    assert buffer.pending_tables() == []


def test_several_tables_are_tracked_independently():
    buffer = CdcBuffer()
    buffer.add(TableSignal("early", SIGNAL_DDL), now=1000.0)
    buffer.add(TableSignal("late", SIGNAL_DDL), now=1050.0)

    assert [e.table_name for e in buffer.take_due(now=1030.0)] == ["early"]
    assert buffer.pending_tables() == ["late"]


def test_the_trigger_summary_reports_what_was_seen():
    buffer = CdcBuffer()
    buffer.add(TableSignal("t", SIGNAL_DDL), now=1000.0)
    buffer.add(TableSignal("t", SIGNAL_ROW), now=1001.0)

    (entry,) = buffer.take_due(now=1100.0)
    assert entry.trigger_summary() == "schema change followed by row writes"
    assert entry.saw_ddl and entry.saw_rows
    assert entry.event_count == 2


# --- Qdrant log parsing -------------------------------------------------------
#
# These lines were captured from qdrant:v1.9.4 by creating a scratch collection,
# inserting two points, deleting one, and dropping the collection. The point of
# the exercise was to establish what the log does NOT contain: a point delete
# leaves an access-log line with no request body, so the deleted ids are simply
# not recoverable from it. That is why the watcher only ever triggers a full
# reconcile.

ACCESS = '2026-08-05T10:00:04.443476Z  INFO actix_web::middleware::logger: 172.18.0.1 "{method} {path} HTTP/1.1" {status} 83 "-" "curl/8.8.0" 0.008300'


def access_line(method: str, path: str, status: int = 200) -> str:
    return ACCESS.format(method=method, path=path, status=status)


def test_a_point_delete_is_detected():
    line = access_line("POST", "/collections/legal_concepts/points/delete?wait=true")

    assert parse_delete_signal(line, "legal_concepts") == DELETE_POINTS


def test_a_vector_delete_is_detected():
    line = access_line("POST", "/collections/legal_concepts/points/vectors/delete?wait=true")

    assert parse_delete_signal(line, "legal_concepts") == DELETE_POINTS


def test_a_collection_drop_is_detected_from_qdrants_own_line():
    line = (
        "2026-08-05T10:00:04.534483Z  INFO "
        "storage::content_manager::toc::collection_meta_ops: "
        "Deleting collection legal_concepts"
    )

    assert parse_delete_signal(line, "legal_concepts") == DELETE_COLLECTION


def test_a_collection_drop_is_detected_from_the_access_line_too():
    line = access_line("DELETE", "/collections/legal_concepts")

    assert parse_delete_signal(line, "legal_concepts") == DELETE_COLLECTION


def test_deletes_on_another_collection_are_ignored():
    """schema_docs is written by the CDC path and must not trigger a concept sync."""
    line = access_line("POST", "/collections/schema_docs/points/delete")

    assert parse_delete_signal(line, "legal_concepts") is None


def test_a_failed_delete_is_ignored():
    """A 4xx changed nothing, so reconciling after it is pure noise."""
    line = access_line("POST", "/collections/legal_concepts/points/delete", status=404)

    assert parse_delete_signal(line, "legal_concepts") is None


def test_writes_are_not_mistaken_for_deletes():
    assert parse_delete_signal(
        access_line("PUT", "/collections/legal_concepts/points?wait=true"), "legal_concepts"
    ) is None
    assert parse_delete_signal(
        access_line("PUT", "/collections/legal_concepts"), "legal_concepts"
    ) is None
    assert parse_delete_signal(
        access_line("POST", "/collections/legal_concepts/points/search"), "legal_concepts"
    ) is None


# --- Deletions in the schema_docs collection ----------------------------------
#
# Deleting a schema document is silent in a way that a concept deletion is not:
# nothing errors, no question fails loudly, the assistant simply stops being
# able to answer anything about that table. The watcher exists to make that
# audible.


def test_a_schema_docs_delete_is_recognised_and_attributed():
    from app.services.qdrant_watch import classify_delete

    line = access_line("POST", "/collections/schema_docs/points/delete")

    assert classify_delete(line) == (DELETE_POINTS, "schema_docs")


def test_the_two_collections_are_told_apart():
    """They are handled differently — concepts reconcile, schema docs only warn —
    so misattributing one would either skip a repair or attempt an unsafe one."""
    from app.services.qdrant_watch import classify_delete

    concepts = access_line("POST", "/collections/legal_concepts/points/delete")
    schema = access_line("POST", "/collections/schema_docs/points/delete")

    assert classify_delete(concepts)[1] == "legal_concepts"
    assert classify_delete(schema)[1] == "schema_docs"


def test_an_unrelated_collection_is_still_ignored():
    from app.services.qdrant_watch import classify_delete

    line = access_line("POST", "/collections/something_else/points/delete")
    kind, collection = classify_delete(line)

    assert kind == DELETE_POINTS
    assert collection == "something_else"
    # The watcher only acts on the two it knows about.
    assert collection not in {"legal_concepts", "schema_docs"}


def test_dropping_the_schema_docs_collection_is_recognised():
    from app.services.qdrant_watch import classify_delete

    line = (
        "2026-08-06T08:39:09Z  INFO storage::content_manager::toc::collection_meta_ops: "
        "Deleting collection schema_docs"
    )

    assert classify_delete(line) == (DELETE_COLLECTION, "schema_docs")


def test_non_delete_traffic_is_not_classified():
    from app.services.qdrant_watch import classify_delete

    assert classify_delete(access_line("POST", "/collections/schema_docs/points/scroll")) == (
        None,
        None,
    )
    assert classify_delete(access_line("PUT", "/collections/schema_docs/points")) == (None, None)
