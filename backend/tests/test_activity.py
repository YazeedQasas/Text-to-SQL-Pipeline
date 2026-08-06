"""Tests for the activity log and the review queue.

Both exist for the same reason: automation removed the person who used to be
watching. So the property that matters for both is that they survive a restart —
an event nobody saw, or a table nobody approved, has to still be there when
someone finally looks.
"""

import json

import pytest

from app.services import activity, review_queue


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    """Point both stores at a temp directory and reset the in-memory ring."""
    monkeypatch.setattr(activity, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(review_queue, "REVIEW_QUEUE_FILE", tmp_path / "review-queue.json")
    activity.reset_for_tests()
    yield
    activity.reset_for_tests()


# --- Activity log -------------------------------------------------------------


def test_an_event_is_readable_immediately():
    activity.record(activity.SOURCE_CDC, "table_documented", "cases is queryable")

    (event,) = activity.recent()
    assert event["message"] == "cases is queryable"
    assert event["source"] == "cdc"
    assert event["level"] == "info"


def test_events_come_back_newest_first():
    for i in range(3):
        activity.record(activity.SOURCE_CDC, "documented", f"table_{i}")

    assert [e["message"] for e in activity.recent()] == ["table_2", "table_1", "table_0"]


def test_events_survive_a_restart():
    """The tab is usually opened *after* something went wrong — and the first
    thing anyone does when something goes wrong is restart the backend."""
    activity.record(activity.SOURCE_CONCEPTS, "sync_applied", "3 concepts synced")

    activity.reset_for_tests()
    assert activity.recent() == []

    activity.load_recent_from_disk()
    assert [e["message"] for e in activity.recent()] == ["3 concepts synced"]


def test_a_corrupt_line_does_not_lose_the_rest(tmp_path):
    path = tmp_path / "activity.jsonl"
    good = {
        "id": "a",
        "ts": 1.0,
        "source": "cdc",
        "level": "info",
        "event": "x",
        "message": "kept",
        "details": {},
    }
    path.write_text(
        json.dumps(good) + "\n{ this is not json\n" + json.dumps({**good, "id": "b"}) + "\n",
        encoding="utf-8",
    )

    activity.reset_for_tests()
    activity.load_recent_from_disk()

    assert len(activity.recent()) == 2


def test_details_ride_along_with_the_event():
    activity.record(
        activity.SOURCE_CDC,
        "documentation_flagged",
        "needs a human",
        level=activity.LEVEL_WARNING,
        table_name="tmp2",
        reasons=["اسم الجدول غير مفهوم"],
    )

    (event,) = activity.recent()
    assert event["details"]["table_name"] == "tmp2"
    assert event["details"]["reasons"] == ["اسم الجدول غير مفهوم"]


def test_warnings_can_be_filtered_and_counted():
    activity.record(activity.SOURCE_CDC, "ok", "fine")
    activity.record(activity.SOURCE_CDC, "flagged", "look at this", level=activity.LEVEL_WARNING)
    activity.record(activity.SOURCE_CDC, "broke", "failed", level=activity.LEVEL_ERROR)

    assert activity.warning_count() == 2
    assert len(activity.recent(level=activity.LEVEL_WARNING)) == 1
    assert len(activity.recent(source=activity.SOURCE_CDC)) == 3


def test_recording_never_raises_when_the_log_cannot_be_written(monkeypatch, tmp_path):
    """Losing the log must not fail the work it was describing.

    A CDC run that documented a table but could not write its log line has still
    documented the table; turning that into an exception would roll back real
    work over bookkeeping.
    """
    monkeypatch.setattr(activity, "ACTIVITY_LOG_FILE", tmp_path / "nope" / "x" / "a.jsonl")
    monkeypatch.setattr(
        activity.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only"))
    )

    event = activity.record(activity.SOURCE_CDC, "documented", "still returned")

    assert event.message == "still returned"
    assert len(activity.recent()) == 1


# --- Review queue -------------------------------------------------------------


def _entry(table_name: str = "tmp2", indexed: bool = False) -> dict:
    return {
        "table_name": table_name,
        "change_type": "table_added",
        "summary": f"New table '{table_name}'",
        "doc": {"table_name": table_name, "description": ""},
        "verdict": {"needs_edit": not indexed, "severity": "unintelligible", "reasons": ["غير مفهوم"]},
        "sample_rows": [{"a": 1}],
        "row_count": 1,
        "indexed": indexed,
    }


def test_a_queued_table_survives_a_restart():
    review_queue.add(**_entry())

    # No in-memory state to reset: it reads the file every time, precisely so a
    # restart between the flag and the review costs nothing.
    (entry,) = review_queue.listing()
    assert entry["table_name"] == "tmp2"
    assert entry["verdict"]["severity"] == "unintelligible"
    assert entry["detected_at"] > 0


def test_re_flagging_a_table_replaces_rather_than_accumulates():
    """"What does this table need?" has one current answer, not a history."""
    review_queue.add(**_entry())
    review_queue.add(**{**_entry(), "summary": "changed again"})

    assert review_queue.count() == 1
    assert review_queue.listing()[0]["summary"] == "changed again"


def test_removing_reports_whether_anything_was_there():
    review_queue.add(**_entry())

    assert review_queue.remove("tmp2") is True
    assert review_queue.remove("tmp2") is False
    assert review_queue.count() == 0


def test_entries_are_listed_oldest_first():
    """The order it should be worked through."""
    review_queue.add(**_entry("first"))
    review_queue.add(**_entry("second"))

    assert [e["table_name"] for e in review_queue.listing()] == ["first", "second"]


def test_a_corrupt_queue_file_does_not_wedge_the_cdc_loop(tmp_path):
    # Must match the filename the fixture points REVIEW_QUEUE_FILE at, or this
    # asserts nothing: a file that does not exist also loads as {}.
    (tmp_path / "review-queue.json").write_text("{ not json", encoding="utf-8")

    assert review_queue.load() == {}
    review_queue.add(**_entry())
    assert review_queue.count() == 1


def test_flagged_entries_are_listed_before_already_indexed_ones():
    """A table that is not queryable yet outranks one that is merely worded
    badly — the first is broken, the second is an improvement."""
    review_queue.add(**_entry("indexed_first", indexed=True))
    review_queue.add(**_entry("flagged_second", indexed=False))

    assert [e["table_name"] for e in review_queue.listing()] == [
        "flagged_second",
        "indexed_first",
    ]


def test_the_two_kinds_are_counted_separately():
    """Only the flagged ones are worth badging in the nav — the indexed ones
    already work."""
    review_queue.add(**_entry("a", indexed=True))
    review_queue.add(**_entry("b", indexed=True))
    review_queue.add(**_entry("c", indexed=False))

    assert review_queue.count() == 3
    assert review_queue.count(indexed=False) == 1
    assert review_queue.count(indexed=True) == 2


def test_clearing_empties_the_log_and_says_so():
    """An empty log and a log somebody emptied look identical otherwise, and the
    difference matters when you are working out why there is no record."""
    for i in range(3):
        activity.record(activity.SOURCE_CDC, "documented", f"table_{i}")

    removed = activity.clear()

    assert removed == 3
    remaining = activity.recent()
    assert len(remaining) == 1
    assert remaining[0]["event"] == "log_cleared"
    assert remaining[0]["details"]["removed"] == 3


def test_clearing_also_empties_the_file_so_a_restart_does_not_bring_it_back():
    activity.record(activity.SOURCE_CDC, "documented", "gone after clearing")
    activity.clear()

    activity.reset_for_tests()
    activity.load_recent_from_disk()

    assert [e["event"] for e in activity.recent()] == ["log_cleared"]
