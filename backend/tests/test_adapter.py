"""Tests for the APC adapter's prompt construction and failure policy.

Whether Llama-3.1-8B is actually GOOD ENOUGH is not decidable here — that needs
the model, and it lives in test_adapter_live.py. What this file pins is
everything around it:

  - the prompt contains what the adapter needs to depart from the template
  - the new question and its required behaviour sit LAST, where truncation
    cannot reach them first
  - the adapter is costed against ITS OWN 8192 window, not the planner's 16000
  - every failure returns None, so the caller falls back to a full pipeline run

The last one is the safety property. A cache that merges questions is only
survivable because the fallback is always available and always correct.
"""

import asyncio

import pytest

from app.services import adapter, llm
from app.services.adapter import PlanTemplate
from app.services.intent import Specific
from app.services.operators import GT, LT, Operator

TEMPLATE = PlanTemplate(
    keyword="case list",
    source_question="ما هي القضايا قبل 2020؟",
    source_sql="SELECT id, title FROM cases WHERE filed_at < '2020-01-01'",
    table_names=["cases"],
)

SCHEMA = "Table: cases\nDescription: القضايا.\nColumns:\n  - id (int, NOT NULL): المعرف.\n"


def _messages(**overrides):
    kwargs = {
        "template": TEMPLATE,
        "question": "ما هي القضايا بعد 2024؟",
        "specifics": [Specific("year", "2024")],
        "schema_context": SCHEMA,
        "glossary": "",
        "operators": [Operator("comparison", "بعد", GT)],
    }
    kwargs.update(overrides)
    return adapter.build_messages(**kwargs)


# --- The prompt carries what is needed to depart from the template ------------


def test_prompt_contains_the_new_question_and_the_worked_example():
    user = _messages()[1]["content"]

    assert "ما هي القضايا بعد 2024؟" in user
    assert TEMPLATE.source_question in user
    assert TEMPLATE.source_sql in user


def test_the_example_is_labelled_as_a_different_question():
    """The anchoring defence. An example presented as "the answer" gets copied."""
    user = _messages()[1]["content"]
    assert "DIFFERENT question" in user

    system = _messages()[0]["content"]
    assert "NEW QUESTION is authoritative" in system


def test_specifics_are_listed_and_required():
    user = _messages(specifics=[Specific("year", "2024"), Specific("role", "المدعى")])[1]["content"]

    assert "year: 2024" in user
    assert "role: المدعى" in user
    assert "MUST appear in your SQL" in user


def test_operator_direction_is_spelled_out_as_an_instruction():
    """The adapter is told to flip the comparison, not left to infer it.

    This is the case the coarse key cannot distinguish and sql_guard cannot
    check on its own, so the instruction has to be explicit.
    """
    user = _messages(operators=[Operator("comparison", "بعد", GT)])[1]["content"]
    assert "LATER-THAN" in user
    assert "never < or <=" in user

    user = _messages(operators=[Operator("comparison", "قبل", LT)])[1]["content"]
    assert "EARLIER-THAN" in user


def test_repeated_operators_are_not_repeated_in_the_prompt():
    user = _messages(
        operators=[Operator("comparison", "بعد", GT), Operator("comparison", "منذ", GT)]
    )[1]["content"]

    assert user.count("LATER-THAN") == 1


def test_a_question_with_no_specifics_says_so_rather_than_leaving_a_hole():
    user = _messages(specifics=[])[1]["content"]
    assert "none — the question names no specific values" in user


def test_the_new_question_sits_after_the_example_and_the_schema():
    """LM Studio truncates from the START, so ordering is a safety property.

    Whatever sits last survives longest, and the last thing must be the new
    question and its required behaviour — never the example.
    """
    user = _messages()[1]["content"]

    assert user.index("Schema:") < user.index("Worked example")
    assert user.index("Worked example") < user.index("The question you must answer now")
    assert user.index("New question:") < user.index("Required behaviour")


def test_the_glossary_is_only_paid_for_when_it_exists():
    without = _messages(glossary="")[1]["content"]
    with_glossary = _messages(glossary="Domain terms: مدورة = carried over.")[1]["content"]

    assert "Domain terms" not in without
    assert "Domain terms" in with_glossary


# --- Budget is the ADAPTER's window, not the planner's ------------------------


def test_a_normal_prompt_fits():
    assert adapter.fits_budget(_messages()) is True


def test_an_oversized_schema_does_not_fit():
    assert adapter.fits_budget(_messages(schema_context="جدول " * 20000)) is False


def test_the_budget_is_the_adapters_window_not_the_planners(monkeypatch):
    """Regression against costing the adapter with services/context.py's limit.

    The planner is loaded at 16000 and the adapter at 8192. A prompt between the
    two fits one and is truncated by the other, and the truncation takes the
    schema off the head of the prompt.
    """
    from app.services import context

    messages = _messages(schema_context="x" * 40000)

    assert context.estimate_messages(messages) < 16000
    assert adapter.fits_budget(messages) is False


# --- Every failure falls back, none of them raises ----------------------------


def _adapt(**overrides):
    kwargs = {
        "template": TEMPLATE,
        "question": "ما هي القضايا بعد 2024؟",
        "specifics": [Specific("year", "2024")],
        "schema_context": SCHEMA,
        "glossary": "",
        "operators": [Operator("comparison", "بعد", GT)],
    }
    kwargs.update(overrides)
    return asyncio.run(adapter.adapt(**kwargs))


def test_a_transport_failure_falls_back(monkeypatch):
    def down(_messages, model=None):
        raise ConnectionError("LM Studio is not running")

    monkeypatch.setattr(llm, "_chat_stream", down)
    assert _adapt() is None


def test_an_empty_response_falls_back(monkeypatch):
    monkeypatch.setattr(llm, "_chat_stream", _fake_stream(""))
    assert _adapt() is None


def test_a_refusal_defers_to_the_full_pipeline(monkeypatch):
    """The adapter saw one example, not the whole schema reasoning path.

    Its refusal is weak evidence, so it must not reach the user — only the
    planner's refusal does.
    """
    monkeypatch.setattr(llm, "_chat_stream", _fake_stream("NO_QUERY: لا تتوفر بيانات."))
    assert _adapt() is None


def test_an_oversized_prompt_falls_back_without_calling_the_model(monkeypatch):
    calls = {"n": 0}

    def counting(_messages, model=None):
        calls["n"] += 1
        return _fake_stream("SELECT 1")(_messages, model)

    monkeypatch.setattr(llm, "_chat_stream", counting)

    assert _adapt(schema_context="جدول " * 20000) is None
    assert calls["n"] == 0, "the budget check must happen before the call"


def test_a_good_response_comes_back_with_fences_stripped(monkeypatch):
    monkeypatch.setattr(
        llm, "_chat_stream", _fake_stream("```sql\nSELECT id FROM cases WHERE a > 1\n```")
    )
    assert _adapt() == "SELECT id FROM cases WHERE a > 1"


def test_the_adapter_model_is_the_one_that_gets_called(monkeypatch):
    """The whole point of the tiering. A regression here silently puts the slow
    reasoning planner back on the fast path."""
    from app.config import ADAPTER_MODEL

    seen = {}

    def capture(messages, model=None):
        seen["model"] = model
        return _fake_stream("SELECT 1")(messages, model)

    monkeypatch.setattr(llm, "_chat_stream", capture)
    _adapt()

    assert seen["model"] == ADAPTER_MODEL


def _fake_stream(text: str):
    def factory(_messages, model=None):
        async def gen():
            yield text

        return gen()

    return factory


def test_adapt_output_is_not_guarded_here():
    """Documents the boundary: this module generates, sql_guard gates.

    If this ever starts returning guarded SQL, the guard has moved and the
    single place that decides what reaches MySQL has become two places.
    """
    assert not hasattr(adapter, "sql_guard")
    assert not hasattr(adapter, "enforce_select_only")
