"""Tests for operator tagging — the words that decide a SQL skeleton.

The suite is built around one asymmetry, and every test is placed on one side of
it or the other:

    a MISSED operator  -> the word stays in the key, keys differ, no merge.
                          Costs a cache hit. Free.
    a TAGGED non-operator -> the word leaves the key, the key gets coarser, and
                          a wrong merge becomes possible.

So the precision tests (`_NOT_OPERATORS`) are the load-bearing ones and the
recall tests are the nice-to-haves. A regression that makes this module tag LESS
is a performance bug; one that makes it tag MORE is a correctness bug.

The non-operator cases are real legal Arabic, not invented: `من قبل` (by),
`لم يُفصل فيها بعد` (not yet), `تحت المراقبة` (a live glossary alias in
data/concepts.json), `حقوق الغير` (third-party rights).
"""

import pytest

from app.services import operators
from app.services.operators import ASC, BETWEEN, COUNT, DESC, EXISTS, GT, LT, NOT


def canon(question: str) -> list[str]:
    return operators.key_tokens(operators.extract_operators(question))


# --- Precision: these must tag NOTHING ----------------------------------------

_NOT_OPERATORS = [
    ("الحكم الصادر من قبل المحكمة", "من قبل = 'by', the agent marker"),
    ("القضايا التي لم يُفصل فيها بعد", "بعد = 'yet', negating nothing"),
    ("ما هي القضايا تحت المراقبة؟", "a glossary alias, not a comparison"),
    ("حقوق الغير في الدعوى", "الغير = 'third party', a noun"),
    ("ما هو الفرق بين المحكمتين؟", "بين with no numeric bounds"),
    ("القرار الصادر بعد المداولة", "بعد + event noun, no operand"),
    ("الجلسات قبل النطق بالحكم", "قبل + event noun, no operand"),
    ("ما هي الدعاوى المشطوبة؟", "no operator at all"),
    ("ما هي المحاكم الموجودة؟", "no operator at all"),
    ("قضايا الاستئناف", "a bare noun phrase"),
]


@pytest.mark.parametrize(("question", "why"), _NOT_OPERATORS)
def test_non_operators_are_not_tagged(question, why):
    """The dangerous direction. A tag here removes a word from the cache key."""
    assert canon(question) == [], f"{why}: {question}"


def test_a_comparison_word_with_no_operand_abstains():
    """The rule that makes the whole thing work: an operator needs an operand."""
    assert canon("القضايا قبل الجلسة") == []
    assert canon("القضايا فوق المتوسط") == []


def test_a_number_too_far_away_is_not_an_operand():
    """A year in a later clause does not make an earlier word a comparison."""
    assert canon("ما هي القضايا قبل الجلسة الأولى المنعقدة في 2020؟") == []


# --- Recall: these must tag correctly -----------------------------------------

_OPERATORS = [
    ("ما هي القضايا قبل 2020؟", [LT]),
    ("ما هي القضايا بعد 2020؟", [GT]),
    ("القضايا فوق 100 يوم", [GT]),
    ("القضايا تحت 30 يوم", [LT]),
    ("ما هي القضايا منذ 2019؟", [GT]),
    ("ما هي القضايا حتى 2019؟", [LT]),
    ("القضايا أكثر من 100 يوم", [GT]),
    ("القضايا أقل من 100 يوم", [LT]),
    ("كم عدد القضايا المدورة؟", [COUNT]),
    ("ما هو عدد الجلسات في 2024؟", [COUNT]),
    ("من هو القاضي الأكثر قضايا؟", [DESC]),
    ("من هو القاضي الأقل قضايا؟", [ASC]),
    ("ما هي أكبر محكمة؟", [DESC]),
    ("ما هي أصغر محكمة؟", [ASC]),
    ("ما هي القضايا غير المدورة؟", [NOT]),
    ("هل توجد قضايا في 2024؟", [EXISTS]),
    ("ما هي القضايا بين 2020 و 2024؟", [BETWEEN]),
]


@pytest.mark.parametrize(("question", "expected"), _OPERATORS)
def test_operators_are_tagged(question, expected):
    assert canon(question) == sorted(expected), question


# --- The pairs the counterexample hunt found -----------------------------------

# Each of these had an IDENTICAL key and IDENTICAL specifics before this module
# existed, and needs structurally different SQL. They are the reason it exists.
_FATAL_PAIRS = [
    ("ما هي القضايا قبل 2020؟", "ما هي القضايا بعد 2020؟", "< vs >"),
    ("القضايا فوق 100 يوم", "القضايا تحت 100 يوم", "above vs below"),
    ("هل توجد قضايا في 2024؟", "كم عدد القضايا في 2024؟", "EXISTS vs COUNT"),
    ("من هو القاضي الأكثر قضايا؟", "من هو القاضي الأقل قضايا؟", "DESC vs ASC"),
    ("ما هي أكبر محكمة؟", "ما هي أصغر محكمة؟", "MAX vs MIN"),
    ("ما هي القضايا المدورة؟", "ما هي القضايا غير المدورة؟", "negation"),
]


@pytest.mark.parametrize(("first", "second", "label"), _FATAL_PAIRS)
def test_the_fatal_merges_now_produce_different_key_tokens(first, second, label):
    """Regression on the wrong-merge class, at the level that fixes it.

    A shared key here is not a slow answer, it is a confident wrong one: the
    cached plan filters the opposite direction and the result looks perfectly
    normal.
    """
    assert canon(first) != canon(second), label


def test_max_min_no_longer_depends_on_the_hamza_accident():
    """أكبر/أصغر used to stay distinct only because arabic.STOPWORDS holds them
    in their hamza form while the key pipeline compares normalized tokens.

    Normalising both sides — an obvious-looking cleanup — would have silently
    turned two safe pairs into wrong merges. Now the distinction is carried by a
    tag rather than by that mismatch, so the cleanup is safe to make.
    """
    assert canon("ما هي أكبر محكمة؟") == [DESC]
    assert canon("ما هي أصغر محكمة؟") == [ASC]


# --- Compound questions --------------------------------------------------------


def test_a_question_can_carry_several_operators():
    """Returning only the first would drop the rest from the key.

    That is the same wrong-merge class again: "كم عدد القضايا بعد 2020" and
    "كم عدد القضايا قبل 2020" would both reduce to `count`.
    """
    assert canon("كم عدد القضايا بعد 2020؟") == sorted([COUNT, GT])
    assert canon("كم عدد القضايا قبل 2020؟") == sorted([COUNT, LT])


def test_compound_operators_still_separate_the_fatal_pair():
    assert canon("كم عدد القضايا بعد 2020؟") != canon("كم عدد القضايا قبل 2020؟")


def test_key_tokens_are_order_insensitive():
    """Two spellings of one range must share a plan.

    The bounds are values and collide by design; the ORDER they were written in
    is not a fact about the plan.
    """
    assert canon("القضايا بعد 2018 وقبل 2020") == canon("القضايا قبل 2020 وبعد 2018")


def test_repeated_operators_collapse():
    assert canon("القضايا بعد 2018 وبعد 2020") == [GT]


def test_extract_keeps_the_surface_form_for_the_trace():
    found = operators.extract_operators("ما هي القضايا بعد 2020؟")
    assert [(o.kind, o.token, o.canonical) for o in found] == [("comparison", "بعد", GT)]


# --- The verification half -----------------------------------------------------


def test_missing_evidence_flags_a_plan_that_dropped_the_operator():
    """The check that catches an adapter reusing the template's comparator."""
    found = operators.extract_operators("ما هي القضايا بعد 2020؟")

    assert operators.missing_evidence(found, "SELECT * FROM cases WHERE filed_at > '2020-01-01'") == []
    assert operators.missing_evidence(found, "SELECT * FROM cases WHERE filed_at < '2020-01-01'") == [GT]


def test_missing_evidence_accepts_the_inclusive_form():
    found = operators.extract_operators("ما هي القضايا بعد 2020؟")
    assert operators.missing_evidence(found, "SELECT * FROM cases WHERE filed_at >= '2020-01-01'") == []


def test_missing_evidence_checks_every_operator():
    found = operators.extract_operators("كم عدد القضايا بعد 2020؟")
    missing = operators.missing_evidence(found, "SELECT id FROM cases WHERE filed_at < '2020-01-01'")
    assert sorted(missing) == sorted([COUNT, GT])


def test_a_question_with_no_operators_asserts_nothing():
    assert operators.missing_evidence([], "SELECT * FROM cases") == []
