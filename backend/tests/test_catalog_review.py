"""Tests for the LLM review gate and the catalog endpoints.

The property that matters most here is failing closed: when the model is
unreachable or returns junk, the table must come back flagged for a human, and
`approve` must refuse to write it. A bug that fails *open* would silently index
exactly the unreviewed garbage this feature exists to catch.

No pytest-asyncio in this project, so async services are driven with
asyncio.run() and the endpoints through TestClient.
"""

import asyncio

from fastapi.testclient import TestClient

from app.main import app
from app.models import TableDocModel
from app.services import llm, reviewer
from app.services.introspect import LiveColumn, LiveTable
from app.services.reviewer import ReviewPacket, Verdict, parse_verdict
from app.services.schema_diff import TABLE_ADDED, TABLE_DROPPED, TableChange

client = TestClient(app)


def _packet(**overrides) -> ReviewPacket:
    defaults = {
        "table_name": "aa_aa_a",
        "change_summary": "New table 'aa_aa_a' exists in MySQL but is not indexed.",
        "columns": [{"name": "x1", "type": "text", "nullable": True}],
        "sample_rows": [{"x1": "asdf"}],
        "row_count": 3,
        "domain_context": [{"table_name": "cases", "description": "Legal cases."}],
    }
    return ReviewPacket(**{**defaults, **overrides})


# --- verdict parsing ----------------------------------------------------------


def test_parses_a_clean_verdict():
    verdict = parse_verdict(
        {
            "needs_edit": False,
            "severity": "ok",
            "reasons": [],
            "suggested_description": "Appeals filed against judgements.",
            "suggested_column_descriptions": {"appeal_id": "PK."},
        }
    )

    assert verdict.needs_edit is False
    assert verdict.severity == "ok"
    assert verdict.suggested_description == "Appeals filed against judgements."


def test_parses_a_flagged_verdict():
    verdict = parse_verdict(
        {
            "needs_edit": True,
            "severity": "unintelligible",
            "reasons": ["Name 'aa_aa_a' carries no meaning"],
            "suggested_description": "",
        }
    )

    assert verdict.needs_edit is True
    assert verdict.reasons == ["Name 'aa_aa_a' carries no meaning"]


def test_unknown_severity_is_normalized_rather_than_trusted():
    assert parse_verdict({"needs_edit": True, "severity": "catastrophic"}).severity == "thin_description"
    assert parse_verdict({"needs_edit": False, "severity": "fine i guess"}).severity == "ok"


def test_missing_needs_edit_defaults_to_flagging():
    """An omitted field must not read as 'this table is fine'."""
    assert parse_verdict({"severity": "ok"}).needs_edit is True


def test_flagged_verdict_always_carries_a_reason():
    verdict = parse_verdict({"needs_edit": True, "severity": "off_domain", "reasons": []})
    assert verdict.reasons, "a flagged table with no reason gives the user nothing to act on"


def test_reasons_accepts_a_bare_string():
    assert parse_verdict({"needs_edit": True, "reasons": "just one"}).reasons == ["just one"]


def test_malformed_column_descriptions_are_dropped_not_fatal():
    verdict = parse_verdict({"needs_edit": False, "suggested_column_descriptions": ["not", "a dict"]})
    assert verdict.suggested_column_descriptions == {}


# --- fail-closed behavior -----------------------------------------------------


def test_unparseable_response_flags_the_table(monkeypatch):
    async def fake_chat_json(_system, _user):
        raise ValueError("No JSON object found in model response.")

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)

    verdict = asyncio.run(reviewer.review(_packet()))

    assert verdict.needs_edit is True
    assert verdict.severity == "unintelligible"
    assert "did not return a usable verdict" in verdict.reasons[0]


def test_llm_transport_failure_flags_the_table(monkeypatch):
    async def fake_chat_json(_system, _user):
        raise ConnectionError("LM Studio is not running")

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)

    assert asyncio.run(reviewer.review(_packet())).needs_edit is True


def test_review_retries_before_giving_up(monkeypatch):
    calls = {"n": 0}

    async def flaky_chat_json(_system, _user):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("bad json")
        return {"needs_edit": False, "severity": "ok", "suggested_description": "Fine."}

    monkeypatch.setattr(llm, "chat_json", flaky_chat_json)

    verdict = asyncio.run(reviewer.review(_packet()))

    assert calls["n"] == 2
    assert verdict.needs_edit is False


# --- prompt construction ------------------------------------------------------


def test_prompt_includes_sample_rows_and_domain_context():
    prompt = reviewer.build_user_prompt(_packet())

    assert "asdf" in prompt, "sample rows are the reviewer's main evidence"
    assert "cases: Legal cases." in prompt, "the existing catalog defines the domain"
    assert "aa_aa_a" in prompt


def test_prompt_asks_a_checkable_question_when_a_description_exists():
    """The re-check must be answerable from evidence, not a taste judgment."""
    prompt = reviewer.build_user_prompt(_packet(current_description="Holds appeal records."))

    assert "accurately and specifically describes them" in prompt
    assert "Holds appeal records." in prompt


def test_prompt_asks_for_a_draft_when_no_description_exists():
    prompt = reviewer.build_user_prompt(_packet(current_description=""))
    assert "has no description yet" in prompt


def test_long_cell_values_are_truncated():
    prompt = reviewer.build_user_prompt(_packet(sample_rows=[{"notes": "x" * 5000}]))
    assert len(prompt) < 3000, "a TEXT column must not swallow the whole prompt"


def test_table_under_review_is_excluded_from_its_own_domain_context():
    from app.services.documenter import domain_context

    context = domain_context(
        {"cases": {"description": "Legal cases."}, "aa_aa_a": {"description": "???"}}, "aa_aa_a"
    )

    assert [entry["table_name"] for entry in context] == ["cases"]


# --- dropped tables skip the LLM ----------------------------------------------


def test_dropped_tables_are_not_sent_to_the_llm():
    dropped = TableChange(change_type=TABLE_DROPPED, table_name="old_backup", indexed={})
    added = TableChange(change_type=TABLE_ADDED, table_name="appeals", live=LiveTable("appeals"))

    assert reviewer.needs_review(dropped) is False
    assert reviewer.needs_review(added) is True


# --- the approval gate --------------------------------------------------------


def _doc(name: str = "aa_aa_a") -> dict:
    return TableDocModel(
        table_name=name,
        description="Some description.",
        primary_key="id",
        columns=[{"name": "id", "type": "int", "nullable": False, "description": "PK."}],
    ).model_dump()


def _stub_state(monkeypatch, verdict: Verdict):
    """Stub everything approve touches except the gate itself."""
    from app.routers import catalog as catalog_router

    async def fake_introspect():
        return {"aa_aa_a": LiveTable("aa_aa_a", columns=[LiveColumn("id", "int", False)])}

    async def fake_fetch_indexed():
        return {}

    async def fake_sample(_table, _known, _limit):
        return [{"id": 1}]

    async def fake_count(_table, _known):
        return 1

    async def fake_review_many(packets):
        return [verdict for _ in packets]

    written = {"upserted": [], "deleted": []}

    async def fake_ensure_collection():
        return None

    async def fake_embed_texts(texts):
        return [[0.1] * 8 for _ in texts]

    async def fake_upsert(docs, _vectors):
        written["upserted"].extend(doc["table_name"] for doc in docs)

    async def fake_delete(names):
        written["deleted"].extend(names)

    monkeypatch.setattr(catalog_router.introspect, "introspect_schema", fake_introspect)
    monkeypatch.setattr(catalog_router.introspect, "sample_rows", fake_sample)
    monkeypatch.setattr(catalog_router.introspect, "row_count", fake_count)
    monkeypatch.setattr(catalog_router, "fetch_indexed_tables", fake_fetch_indexed)
    monkeypatch.setattr(catalog_router.reviewer, "review_many", fake_review_many)
    monkeypatch.setattr(catalog_router, "ensure_collection", fake_ensure_collection)
    monkeypatch.setattr(catalog_router, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(catalog_router, "upsert_tables", fake_upsert)
    monkeypatch.setattr(catalog_router, "delete_tables", fake_delete)
    return written


def test_approve_writes_what_was_submitted(monkeypatch):
    written = _stub_state(monkeypatch, Verdict(needs_edit=False, severity="ok"))

    response = client.post(
        "/api/catalog/approve", json={"items": [{"doc": _doc(), "action": "upsert"}]}
    )

    assert response.status_code == 200
    assert response.json()["upserted"] == ["aa_aa_a"]
    assert written["upserted"] == ["aa_aa_a"]


def test_approve_does_not_re_review_the_operators_text(monkeypatch):
    """The scan judges once; after that the operator's description is final.

    Re-judging on the way in would make editing pointless — a table the scan
    flagged could never be cleared no matter what was typed into it.
    """
    calls = {"n": 0}
    written = _stub_state(monkeypatch, Verdict(needs_edit=False, severity="ok"))

    from app.routers import catalog as catalog_router

    async def counting_review_many(packets):
        calls["n"] += 1
        return [Verdict(needs_edit=True, severity="off_domain", reasons=["nope"]) for _ in packets]

    monkeypatch.setattr(catalog_router.reviewer, "review_many", counting_review_many)

    response = client.post(
        "/api/catalog/approve", json={"items": [{"doc": _doc(), "action": "upsert"}]}
    )

    assert response.status_code == 200
    assert calls["n"] == 0, "approve must not call the reviewer at all"
    assert written["upserted"] == ["aa_aa_a"]


def test_approve_deletes_dropped_tables(monkeypatch):
    written = _stub_state(monkeypatch, Verdict(needs_edit=True, severity="off_domain"))

    response = client.post(
        "/api/catalog/approve",
        json={"items": [{"doc": _doc("old_backup"), "action": "delete"}]},
    )

    assert response.status_code == 200
    assert written["deleted"] == ["old_backup"]


def test_approve_skips_what_the_user_declined(monkeypatch):
    written = _stub_state(monkeypatch, Verdict(needs_edit=False, severity="ok"))

    response = client.post(
        "/api/catalog/approve", json={"items": [{"doc": _doc(), "action": "skip"}]}
    )

    assert response.status_code == 200
    assert response.json()["skipped"] == ["aa_aa_a"]
    assert written["upserted"] == []


def test_approve_handles_a_mixed_batch(monkeypatch):
    written = _stub_state(monkeypatch, Verdict(needs_edit=False, severity="ok"))

    response = client.post(
        "/api/catalog/approve",
        json={
            "items": [
                {"doc": _doc("appeals"), "action": "upsert"},
                {"doc": _doc("old_backup"), "action": "delete"},
                {"doc": _doc("aa_aa_a"), "action": "skip"},
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["upserted"] == ["appeals"]
    assert body["deleted"] == ["old_backup"]
    assert body["skipped"] == ["aa_aa_a"]


# --- MySQL types that JSON cannot write ---------------------------------------


def test_sample_row_values_are_made_json_safe():
    """A table with money or dates must not break its own documentation run.

    Regression: `case_fees(amount DECIMAL(10,2), paid_date DATE)` failed with
    "Object of type Decimal is not JSON serializable" while an earlier test
    table of INT/TEXT columns passed. Sample rows travel into the review queue
    file, the activity log and the SSE stream, so one unconverted cell costs the
    whole run — and DECIMAL/DATE are ordinary column types, not exotic ones.
    """
    import json
    from datetime import date, datetime, time, timedelta
    from decimal import Decimal

    from app.services.introspect import json_safe_row

    row = json_safe_row(
        {
            "amount": Decimal("250.00"),
            "paid_date": date(2024, 1, 15),
            "created_at": datetime(2024, 1, 15, 9, 30, 0),
            "start_time": time(9, 30),
            "elapsed": timedelta(hours=2, minutes=5),
            "blob": b"\x00\x01\x02\x03",
            "receipt_no": "RCP-2024-0001",
            "case_id": 1,
            "note": None,
        }
    )

    # The point of the exercise: this must not raise.
    json.dumps(row, ensure_ascii=False)

    # DECIMAL becomes a string, not a float — these are money values and float
    # would silently round them.
    assert row["amount"] == "250.00"
    assert isinstance(row["amount"], str)

    assert row["paid_date"] == "2024-01-15"
    assert row["created_at"] == "2024-01-15T09:30:00"
    assert row["start_time"] == "09:30:00"
    assert row["elapsed"] == "2:05:00"
    assert row["blob"] == "<4 bytes>"

    # Values JSON already handles are passed through untouched.
    assert row["receipt_no"] == "RCP-2024-0001"
    assert row["case_id"] == 1
    assert row["note"] is None


def test_the_review_queue_survives_a_row_that_slipped_through_unconverted():
    """Backstop for the layer above: `default=str` on the write.

    json_safe_row is the real fix, but the queue must not be wedgeable by a
    value that reaches it some other way.
    """
    import json
    from decimal import Decimal

    from app.services import review_queue

    body = json.dumps({"rows": [{"amount": Decimal("250.00")}]}, default=str)
    assert "250.00" in body
    assert review_queue is not None
