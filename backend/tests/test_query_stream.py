"""End-to-end tests for the query pipeline with all external services stubbed.

The pipeline imports `embed_text` / `search_tables` / `execute_select` by name,
so those are patched on `app.services.pipeline`; `llm` is imported as a module
and patched there.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import context, llm, pipeline, retrieval
from app.services.retrieval import SchemaTable

client = TestClient(app)


def _table() -> SchemaTable:
    return SchemaTable(
        table_name="cases",
        description="Legal cases.",
        primary_key="case_id",
        columns=[{"name": "case_id", "type": "INT", "nullable": False, "description": "PK"}],
        foreign_keys=[],
        score=0.91,
    )


async def _chunks(*parts: str):
    for part in parts:
        yield part


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Stub every external call the pipeline makes; return the knobs tests tweak."""
    state = {
        "sql_parts": ["SELECT case_id ", "FROM cases"],
        "tables": [_table()],
        "rows": [],
        # Recorded so tests can assert what the pipeline passed downstream.
        "carried": None,
        "sql_history": None,
        "answer_history": None,
        # Empty by default: no matched concept is the ordinary turn, so that is
        # what the rest of these tests should be exercising.
        "concepts": [],
        "boosted_with": None,
        "sql_glossary": None,
        "grams": None,
    }

    async def fake_embed_texts(texts):
        # One vector per input: the question first, then its fragments.
        return [[0.1] * 8 for _ in texts]

    async def fake_search_concepts(_question, gram_vectors):
        state["grams"] = [gram for gram, _ in gram_vectors]
        return state["concepts"]

    async def fake_search_with_carryover(_vector, carry_table_names, concepts=()):
        state["carried"] = carry_table_names
        state["boosted_with"] = list(concepts)
        return state["tables"]

    async def fake_execute_select(_sql):
        return ["case_id"], state["rows"]

    def fake_stream_sql(_question, _schema, history=(), glossary=""):
        state["sql_history"] = list(history)
        state["sql_glossary"] = glossary
        return _chunks(*state["sql_parts"])

    def fake_stream_answer(_question, _sql, _columns, _rows, history=()):
        state["answer_history"] = list(history)
        return _chunks("One ", "case ", "found.")

    monkeypatch.setattr(pipeline, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(pipeline, "search_concepts", fake_search_concepts)
    monkeypatch.setattr(pipeline, "search_with_carryover", fake_search_with_carryover)
    monkeypatch.setattr(pipeline, "execute_select", fake_execute_select)
    monkeypatch.setattr(llm, "stream_sql", fake_stream_sql)
    monkeypatch.setattr(llm, "stream_answer", fake_stream_answer)
    return state


def _events(question: str = "how many cases?", history: list | None = None) -> list[dict]:
    payload = {"question": question, "history": history or []}
    with client.stream("POST", "/api/query/stream", json=payload) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    return [
        json.loads(frame[len("data:") :].strip())
        for frame in body.split("\n\n")
        if frame.startswith("data:")
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NO_QUERY: لا تتضمن البيانات معلومات عن الطقس.", "لا تتضمن البيانات معلومات عن الطقس."),
        ("NO_QUERY - no weather data", "no weather data"),
        ("no_query: لا توجد بيانات.", "لا توجد بيانات."),  # models lowercase it
        ("NO_QUERY", llm.NO_QUERY_FALLBACK),
        ('"NO_QUERY: لا توجد بيانات."', "لا توجد بيانات."),  # and quote it
    ],
)
def test_parse_no_query_extracts_the_reason(raw, expected):
    assert llm.parse_no_query(raw) == expected


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT case_id FROM cases",
        # The token appears in the query, but the query is real — must still run.
        "SELECT case_id FROM cases WHERE title LIKE '%NO_QUERY%'",
    ],
)
def test_parse_no_query_leaves_real_sql_alone(sql):
    assert llm.parse_no_query(sql) is None


def test_stream_reports_every_stage_in_order(stub_pipeline):
    events = _events()

    assert events[0]["type"] == "stages"
    assert [s["id"] for s in events[0]["stages"]] == [s["id"] for s in pipeline.STAGES]

    started = [e["stage"] for e in events if e["type"] == "stage" and e["status"] == "started"]
    completed = [e["stage"] for e in events if e["type"] == "stage" and e["status"] == "completed"]
    expected = [s["id"] for s in pipeline.STAGES]
    assert started == expected
    assert completed == expected


def test_stream_forwards_llm_tokens_and_final_result(stub_pipeline):
    events = _events()

    sql_tokens = [e["text"] for e in events if e["type"] == "token" and e["stage"] == "sql_generation"]
    answer_tokens = [
        e["text"] for e in events if e["type"] == "token" and e["stage"] == "answer_generation"
    ]
    assert "".join(sql_tokens) == "SELECT case_id FROM cases"
    assert "".join(answer_tokens) == "One case found."

    result = next(e["result"] for e in events if e["type"] == "result")
    assert result["answer"] == "One case found."
    assert result["sql"].startswith("SELECT case_id FROM cases")
    assert result["retrieved_tables"][0]["table_name"] == "cases"


def test_stream_reports_failure_in_band(stub_pipeline):
    stub_pipeline["tables"] = []
    events = _events()

    error = next(e for e in events if e["type"] == "error")
    assert error["status_code"] == 422
    assert "No relevant tables" in error["detail"]
    # The stage that failed announced its start but never completed.
    assert {
        "type": "stage",
        "stage": "retrieval",
        "status": "started",
        "detail": None,
        "content": None,
    } in events
    assert not any(
        e["type"] == "stage" and e["stage"] == "retrieval" and e["status"] == "completed"
        for e in events
    )


def test_refusal_is_reported_with_the_models_own_reason(stub_pipeline):
    """A question the schema cannot answer stops at SQL generation."""
    stub_pipeline["sql_parts"] = ["NO_QUERY: ", "لا تتضمن البيانات معلومات عن الطقس."]

    events = _events("ما هو الطقس في عمّان؟")

    error = next(e for e in events if e["type"] == "error")
    assert error["status_code"] == 422
    assert error["detail"] == "لا تتضمن البيانات معلومات عن الطقس."
    # It stopped there: the SQL was never validated or run.
    assert not any(
        e["type"] == "stage" and e["stage"] in {"sql_validation", "sql_execution"} for e in events
    )


def test_refusal_without_a_reason_still_says_something_usable(stub_pipeline):
    stub_pipeline["sql_parts"] = ["NO_QUERY"]

    error = next(e for e in _events() if e["type"] == "error")

    assert error["detail"] == llm.NO_QUERY_FALLBACK


# --- Conversation memory ------------------------------------------------------


def _history(**overrides) -> dict:
    return {
        "question": "ما هي القضايا التي نظرها القاضي سامي البرغوثي؟",
        "sql": "SELECT case_id FROM cases WHERE judge_id = 4",
        "answer": "ثلاث قضايا.",
        "table_names": ["cases", "judges"],
        **overrides,
    }


def test_history_reaches_both_llm_calls_and_retrieval(stub_pipeline):
    _events("ومين كان القاضي؟", history=[_history()])

    # The follow-up names no table of its own, so the previous turn's tables are
    # what keeps the conversation's subject in the prompt.
    assert stub_pipeline["carried"] == ["cases", "judges"]
    assert [turn.sql for turn in stub_pipeline["sql_history"]] == [
        "SELECT case_id FROM cases WHERE judge_id = 4"
    ]
    assert [turn.answer for turn in stub_pipeline["answer_history"]] == ["ثلاث قضايا."]


def test_first_question_carries_nothing(stub_pipeline):
    _events()

    assert stub_pipeline["carried"] == []
    assert stub_pipeline["sql_history"] == []


def test_usage_is_emitted_mid_run_even_when_the_turn_then_fails(stub_pipeline):
    """The meter must be right on the turn that fails, not only on success."""
    stub_pipeline["sql_parts"] = ["NO_QUERY: لا توجد بيانات."]

    events = _events(history=[_history()])

    usage = next(e["usage"] for e in events if e["type"] == "usage")
    assert usage["used_tokens"] > 0
    assert usage["history_turns"] == 1
    assert any(e["type"] == "error" for e in events)


def test_usage_reports_the_window_the_next_turn_starts_from(stub_pipeline):
    result = next(e["result"] for e in _events(history=[_history()]) if e["type"] == "result")

    usage = result["usage"]
    assert usage["limit_tokens"] == context.limit_tokens()
    assert 0 < usage["used_tokens"] < usage["limit_tokens"]
    # One turn was replayed; this exchange is about to become the second.
    assert usage["history_turns"] == 2


def test_context_cutoff_refuses_before_the_model_truncates(stub_pipeline, monkeypatch):
    # A window with no room for anything: every request overflows it.
    monkeypatch.setattr(context, "limit_tokens", lambda: 1)

    events = _events(history=[_history()])

    error = next(e for e in events if e["type"] == "error")
    assert error["status_code"] == 413
    assert "امتلأت ذاكرة المحادثة" in error["detail"]
    # It stopped at the prompt stage, before spending an LLM call.
    assert not any(e["type"] == "token" for e in events)
    assert not any(
        e["type"] == "stage" and e["stage"] == "sql_generation" for e in events
    )


def test_history_is_trimmed_to_the_configured_turn_cap(stub_pipeline, monkeypatch):
    monkeypatch.setattr(context, "CONTEXT_MAX_TURNS", 2)

    _events(history=[_history(sql=f"SELECT {n}") for n in range(5)])

    assert [turn.sql for turn in stub_pipeline["sql_history"]] == ["SELECT 3", "SELECT 4"]


def test_non_streaming_endpoint_still_returns_json(stub_pipeline):
    response = client.post("/api/query", json={"question": "how many cases?"})

    assert response.status_code == 200
    assert response.json()["answer"] == "One case found."


def test_non_streaming_endpoint_maps_pipeline_error_to_http_status(stub_pipeline):
    stub_pipeline["tables"] = []

    response = client.post("/api/query", json={"question": "how many cases?"})

    assert response.status_code == 422
    assert "No relevant tables" in response.json()["detail"]


# --- Domain concepts ---------------------------------------------------------


def _concept(term: str = "القضية المختنقة", tables: list | None = None):
    return retrieval.Concept(
        term=term,
        aliases=["مختنقة"],
        definition="الدعوى التي تجاوزت المدة المقررة.",
        sql="case_congestion.congestion_status = 'مختنقة'",
        tables=tables if tables is not None else ["case_congestion"],
        score=0.73,
    )


def test_no_matched_concept_says_so_and_leaves_the_prompt_untouched(stub_pipeline):
    """The ordinary turn: the glossary must cost nothing and change nothing."""
    events = _events()

    stage = next(
        e for e in events
        if e["type"] == "stage" and e["stage"] == "concepts" and e["status"] == "completed"
    )
    assert stage["detail"] == "no domain terms matched"
    assert stage.get("content") is None
    assert stub_pipeline["sql_glossary"] == ""
    assert stub_pipeline["boosted_with"] == []


def test_matched_concept_reaches_both_retrieval_and_the_sql_prompt(stub_pipeline):
    """A concept is useless if it steers retrieval but never reaches the model."""
    stub_pipeline["concepts"] = [_concept()]

    events = _events()

    # It biased which tables were retrieved...
    assert [c.term for c in stub_pipeline["boosted_with"]] == ["القضية المختنقة"]
    # ...and the fragment the model must reuse actually reached the prompt.
    assert "case_congestion.congestion_status = 'مختنقة'" in stub_pipeline["sql_glossary"]

    stage = next(
        e for e in events
        if e["type"] == "stage" and e["stage"] == "concepts" and e["status"] == "completed"
    )
    assert "القضية المختنقة" in stage["detail"]
    assert "0.73" in stage["detail"]
    assert "case_congestion" in stage["content"]


def test_glossary_is_counted_against_the_context_budget(stub_pipeline):
    """It rides in the prompt, so it must be paid for before the model sees it."""
    without = next(e for e in _events() if e["type"] == "usage")["usage"]["used_tokens"]

    stub_pipeline["concepts"] = [_concept()]
    with_glossary = next(e for e in _events() if e["type"] == "usage")["usage"]["used_tokens"]

    assert with_glossary > without
