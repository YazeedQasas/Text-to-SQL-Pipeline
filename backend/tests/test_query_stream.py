"""End-to-end tests for the query pipeline with all external services stubbed.

The pipeline imports `embed_text` / `search_tables` / `execute_select` by name,
so those are patched on `app.services.pipeline`; `llm` is imported as a module
and patched there.
"""

import asyncio
import json

import fakeredis
import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import catalog, context, llm, pipeline, query_cache, retrieval
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


@pytest.fixture(autouse=True)
def isolated_query_cache():
    """Give every test its own in-process Redis for the question cache.

    Autouse because the client is module-global: without it these tests would
    talk to whatever real Redis is on the machine, and each test asking the same
    question would be served from a previous test's entry rather than from its
    own stubs.
    """
    server = fakeredis.FakeServer()
    query_cache.set_client(fakeredis.aioredis.FakeRedis(server=server, decode_responses=True))
    yield server
    query_cache.set_client(None)


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Stub every external call the pipeline makes; return the knobs tests tweak."""
    # These tests exercise the GENERATION path, and several of them run the same
    # question twice to compare two outcomes. With the cache on, the second run
    # would be answered from the first and would assert against stubs that were
    # never reached. The cache path has its own tests at the end of this file.
    monkeypatch.setattr(pipeline, "QUERY_CACHE_ENABLED", False)

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
        # How often the expensive halves actually ran, so a cache test can show
        # that generation was skipped while execution was not.
        "sql_calls": 0,
        "execute_calls": 0,
        "fetched_by_name": None,
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
        state["execute_calls"] += 1
        return ["case_id"], state["rows"]

    async def fake_fetch_tables_by_name(names):
        state["fetched_by_name"] = list(names)
        return [table for table in state["tables"] if table.table_name in names]

    async def fake_concepts_by_terms(terms):
        return [concept for concept in state["concepts"] if concept.term in terms]

    def fake_stream_sql(_question, _schema, history=(), glossary=""):
        state["sql_calls"] += 1
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
    monkeypatch.setattr(pipeline, "fetch_tables_by_name", fake_fetch_tables_by_name)
    monkeypatch.setattr(pipeline, "concepts_by_terms", fake_concepts_by_terms)
    monkeypatch.setattr(llm, "stream_sql", fake_stream_sql)
    monkeypatch.setattr(llm, "stream_answer", fake_stream_answer)
    return state


@pytest.fixture
def cached_pipeline(stub_pipeline, monkeypatch):
    """`stub_pipeline`, with the repeated-question cache switched back on."""
    monkeypatch.setattr(pipeline, "QUERY_CACHE_ENABLED", True)
    return stub_pipeline


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
    assert error["detail"] == pipeline.NO_TABLES_MESSAGE
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
    assert response.json()["detail"] == pipeline.NO_TABLES_MESSAGE


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
    assert stage["detail"] == "لم تُطابق أي مصطلحات قانونية"
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
    # The user-facing stage text names the term and nothing else — no score, no
    # table name. The glossary behind it still carries the SQL the model needs.
    assert stage["detail"] == "القضية المختنقة"
    assert "case_congestion" in stage["content"]


def test_glossary_is_counted_against_the_context_budget(stub_pipeline):
    """It rides in the prompt, so it must be paid for before the model sees it."""
    without = next(e for e in _events() if e["type"] == "usage")["usage"]["used_tokens"]

    stub_pipeline["concepts"] = [_concept()]
    with_glossary = next(e for e in _events() if e["type"] == "usage")["usage"]["used_tokens"]

    assert with_glossary > without


# --- The repeated-question cache ----------------------------------------------


def test_a_repeated_question_skips_generation_but_still_runs_the_query(cached_pipeline):
    """The whole point: reuse the QUERY, never its results.

    Asking twice must cost one SQL generation and two executions — the second
    answer has to come from whatever the database holds now, not from what it
    held the first time.
    """
    _events("كم عدد القضايا؟")
    _events("كم عدد القضايا؟")

    assert cached_pipeline["sql_calls"] == 1
    assert cached_pipeline["execute_calls"] == 2


def test_the_cache_ignores_punctuation_the_way_a_reader_would(cached_pipeline):
    _events("كم عدد القضايا؟")
    _events("كم عدد القضايا")

    assert cached_pipeline["sql_calls"] == 1


def test_a_reused_answer_still_reports_its_tables(cached_pipeline):
    """Follow-ups carry the previous turn's tables, so a cached turn that
    reported none would silently break the next question."""
    _events("كم عدد القضايا؟")
    result = next(e for e in _events("كم عدد القضايا؟") if e["type"] == "result")["result"]

    assert [t["table_name"] for t in result["retrieved_tables"]] == ["cases"]
    assert cached_pipeline["fetched_by_name"] == ["cases"]


def test_a_reused_answer_marks_the_stages_it_skipped(cached_pipeline):
    _events("كم عدد القضايا؟")
    events = _events("كم عدد القضايا؟")

    generation = next(
        e for e in events
        if e["type"] == "stage" and e["stage"] == "sql_generation" and e["status"] == "completed"
    )
    assert generation["detail"] == pipeline.REUSED_DETAIL
    # The stages that genuinely ran must not claim to have been reused.
    execution = next(
        e for e in events
        if e["type"] == "stage" and e["stage"] == "sql_execution" and e["status"] == "completed"
    )
    assert execution["detail"] != pipeline.REUSED_DETAIL


def _prior_turn(question="كم عدد القضايا؟"):
    """One completed turn, as the browser would replay it."""
    return [_history(question=question, sql="SELECT case_id FROM cases", answer="One.")]


def test_a_follow_up_does_not_reuse_a_cached_query_by_default(cached_pipeline):
    """QUERY_CACHE_FOLLOW_UPS defaults FALSE, so a chat with prior turns re-derives.

    This test used to assert the opposite. The justification for allowing reads on
    any turn was that everything stored is "context-free by construction" — but
    writing is gated on the turn's POSITION, not the question's CONTENT, so an
    anaphoric question asked as a first turn does reach the cache. The key is the
    question alone, with no chat id and no history in it, so a permissive read
    lets one conversation run SQL built for another's cold opening.

    Two generation calls is the cost being paid for that, and it is most of the
    hit rate. See config.QUERY_CACHE_FOLLOW_UPS.
    """
    _events("كم عدد القضايا؟")
    _events("كم عدد القضايا؟", history=_prior_turn("سؤال آخر تمامًا"))

    assert cached_pipeline["sql_calls"] == 2


def test_follow_up_reuse_can_be_opted_into(cached_pipeline, monkeypatch):
    """The hit rate is available to a deployment that accepts the trade."""
    monkeypatch.setattr(pipeline, "QUERY_CACHE_FOLLOW_UPS", True)

    _events("كم عدد القضايا؟")
    _events("كم عدد القضايا؟", history=_prior_turn("سؤال آخر تمامًا"))

    assert cached_pipeline["sql_calls"] == 1


def test_a_first_turn_still_reuses_whatever_the_follow_up_setting(cached_pipeline):
    """The gate is `FOLLOW_UPS or not has_prior_turns` — a cold ask always reads."""
    _events("كم عدد القضايا؟")
    _events("كم عدد القضايا؟")

    assert cached_pipeline["sql_calls"] == 1


def test_a_non_positive_turn_cap_does_not_reopen_the_write_guard(
    cached_pipeline, monkeypatch
):
    """CONTEXT_MAX_TURNS <= 0 must not turn "first turns only" into "store everything".

    trim_history returns [] for any non-positive cap, so a follow-up used to
    arrive at the write guard looking like a first turn. run_pipeline now decides
    from the untrimmed input, so a context knob can no longer reach cache policy.
    """
    monkeypatch.setattr(context, "CONTEXT_MAX_TURNS", 0)

    # A follow-up whose wording depends on the turn before it.
    _events("وكم منها مفتوحة؟", history=_prior_turn())

    assert asyncio.run(query_cache.stats())["entries"] == 0


def test_a_follow_up_answer_is_never_written_to_the_cache(cached_pipeline):
    """The invariant the read side depends on: everything stored is context-free.

    An answer produced with a conversation behind it can mean something its
    wording alone does not, so it must never become an entry a different
    conversation could pick up.
    """
    _events("وكم في غزة؟", history=_prior_turn())

    assert asyncio.run(query_cache.stats())["entries"] == 0

    # And asking it again as a follow-up still regenerates, because nothing
    # was ever stored for it.
    _events("وكم في غزة؟", history=_prior_turn())
    assert cached_pipeline["sql_calls"] == 2


def test_a_refused_question_is_not_cached(cached_pipeline):
    """A refusal says nothing about whether the retrieval behind it was right."""
    cached_pipeline["sql_parts"] = ["NO_QUERY: لا توجد بيانات."]
    _events("سؤال بلا إجابة")
    _events("سؤال بلا إجابة")

    assert cached_pipeline["sql_calls"] == 2


def test_documenting_a_table_drops_the_cache(cached_pipeline):
    """Stored SQL was written against the schema as it was."""
    _events("كم عدد القضايا؟")
    assert asyncio.run(query_cache.stats())["entries"] == 1

    asyncio.run(catalog._invalidate_query_cache("a table was documented"))

    _events("كم عدد القضايا؟")
    assert cached_pipeline["sql_calls"] == 2


def test_a_dead_redis_still_answers_the_question(cached_pipeline, monkeypatch):
    """The cache is an optimisation. Losing Redis costs the optimisation and
    nothing else — every question simply runs the full pipeline."""

    class _DeadRedis:
        def __getattr__(self, _name):
            def boom(*_args, **_kwargs):
                raise ConnectionError("Connection refused")

            return boom

    monkeypatch.setattr(query_cache, "_client", _DeadRedis())

    result = next(e for e in _events("كم عدد القضايا؟") if e["type"] == "result")["result"]

    assert result["answer"] == "One case found."
    assert cached_pipeline["sql_calls"] == 1
