"""Tests for the conversation token budget.

The estimates here are approximations by design, so these tests pin the
properties the budget depends on rather than exact counts: Arabic must cost
more per character than Latin, nothing non-empty may be free, and the estimate
must never come in under the real cost in a way that hands overflow to
LM Studio.
"""

import pytest

from app.services import context

ARABIC = "ما هي القضايا المفتوحة في محكمة عمان الابتدائية؟"
LATIN = "SELECT case_number FROM cases WHERE status = 'open' ORDER BY filing_date"


def test_empty_text_is_free():
    assert context.estimate_tokens("") == 0


def test_any_content_costs_at_least_one_token():
    assert context.estimate_tokens("a") >= 1
    assert context.estimate_tokens("ق") >= 1


def test_arabic_costs_more_per_character_than_latin():
    """Arabic tokenizes denser, and under-counting it is the dangerous direction."""
    arabic_per_char = context.estimate_tokens(ARABIC) / len(ARABIC)
    latin_per_char = context.estimate_tokens(LATIN) / len(LATIN)

    assert arabic_per_char > latin_per_char


def test_estimate_grows_with_length():
    assert context.estimate_tokens(ARABIC * 3) > context.estimate_tokens(ARABIC)


def test_message_framing_is_charged_per_message():
    """Ten short messages must not be costed as one concatenated string."""
    single = context.estimate_messages([{"role": "user", "content": "x" * 100}])
    split = context.estimate_messages([{"role": "user", "content": "x" * 10}] * 10)

    assert split > single


def test_limit_leaves_room_for_the_reply():
    from app.config import CONTEXT_WINDOW_TOKENS

    assert 0 < context.limit_tokens() < CONTEXT_WINDOW_TOKENS


@pytest.mark.parametrize("length", [0, 1, 5, 40])
def test_trim_history_keeps_the_most_recent_turns(monkeypatch, length):
    monkeypatch.setattr(context, "CONTEXT_MAX_TURNS", 3)
    history = list(range(length))

    trimmed = context.trim_history(history)

    assert trimmed == history[-3:]
    assert len(trimmed) <= 3


def test_measure_flags_exhaustion_against_the_limit(monkeypatch):
    monkeypatch.setattr(context, "limit_tokens", lambda: 10)

    assert context.measure([{"role": "user", "content": "hi"}], 0).exhausted is False
    assert context.measure([{"role": "user", "content": ARABIC * 20}], 4).exhausted is True


def test_result_rows_are_fitted_to_the_remaining_budget(monkeypatch):
    """Rows are the one prompt input whose size the user controls."""
    from app.services import llm

    wide_rows = [{"summary": "ملخص طويل جدا " * 40, "case_id": n} for n in range(50)]

    monkeypatch.setattr(context, "limit_tokens", lambda: 2000)
    messages = llm.build_answer_messages("سؤال", "SELECT 1", ["case_id"], wide_rows)

    assert context.estimate_messages(messages) <= 2000
    # The row count is still reported honestly even though few are shown.
    assert "50 total" in messages[-1]["content"]


def test_small_results_are_not_trimmed():
    from app.services import llm

    rows = [{"case_id": n} for n in range(3)]
    content = llm.build_answer_messages("سؤال", "SELECT 1", ["case_id"], rows)[-1]["content"]

    assert "3 total)" in content  # no "showing N" qualifier
    assert "'case_id': 2" in content


def test_budget_fraction_is_bounded_at_zero_limit(monkeypatch):
    """A misconfigured window must not divide by zero on the way to refusing."""
    monkeypatch.setattr(context, "limit_tokens", lambda: 0)

    budget = context.measure([{"role": "user", "content": "x"}], 0)

    assert budget.fraction == 0.0
    assert budget.exhausted is True
