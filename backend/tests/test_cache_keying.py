"""Tests for what may and may not share a cache key, and who may read one.

Three defects are pinned here, all of them the same underlying mistake: two
different questions being treated as one because the thing that distinguished
them got erased.

  1. arabic.normalize folds ى→ي and ة→ه, which merged "المدعي" (plaintiff) with
     "المدعى" (the sued) onto one key. cache_key now uses normalize_key.
  2. The cache's write guard read the TRIMMED history, so CONTEXT_MAX_TURNS <= 0
     made every turn look like a first turn and stored everything.
  3. Follow-up reads were on by default, letting one chat run SQL cached from
     another chat's cold opening.

The collision cases are written as pairs of real questions rather than as
character-level assertions, because the property that matters is about
questions, not about characters.
"""

import asyncio

import fakeredis.aioredis
import pytest

from app.models import HistoryTurn
from app.services import arabic, context, query_cache


@pytest.fixture(autouse=True)
def fake_redis():
    query_cache.set_client(fakeredis.aioredis.FakeRedis(decode_responses=True))
    yield
    asyncio.run(query_cache.close())


# --- The two normalizers are different on purpose -----------------------------

# Pairs that name DIFFERENT things and must never share a key. Each was measured
# colliding under the soft normalizer.
DISTINCT_PAIRS = [
    ("من هو المدعي؟", "من هو المدعى؟", "plaintiff vs the sued"),
    ("قضايا المدعي", "قضايا المدعى", "plaintiff's cases vs the sued's"),
    ("كم قضية على القاضي؟", "كم قضية علي القاضي؟", "preposition vs given name"),
    ("كتابة العقد", "كتابه العقد", "drafting the contract vs his contract book"),
    ("محاكمة المتهم", "محاكمه المتهم", "the trial vs his court"),
]

# Pairs that mean the SAME thing and must still share a key — the folds that were
# kept. This is the half of the trade that the strict normalizer must not break.
SAME_PAIRS = [
    ("ما هي القضايا المدورة؟", "ما هي قضايا مدورة", "definite article + punctuation"),
    ("محـــكمة الصلح", "محكمة الصلح", "tatweel"),
    ("أحمد", "احمد", "hamza carrier omitted"),
    ("كم عدد القضايا؟", "كم  عدد   القضايا ؟", "whitespace"),
]


@pytest.mark.parametrize(("first", "second", "label"), DISTINCT_PAIRS)
def test_distinct_questions_get_distinct_keys(first, second, label):
    assert query_cache.cache_key(first) != query_cache.cache_key(second), label


@pytest.mark.parametrize(("first", "second", "label"), SAME_PAIRS)
def test_equivalent_questions_still_share_a_key(first, second, label):
    assert query_cache.cache_key(first) == query_cache.cache_key(second), label


@pytest.mark.parametrize(("first", "second", "label"), DISTINCT_PAIRS)
def test_the_soft_normalizer_still_merges_these(first, second, label):
    """Documents WHY the split exists.

    If this ever fails, arabic.normalize has been tightened and the two
    normalizers may be able to collapse back into one — but check the *_norm
    columns in db/01_schema.sql first, because normalize() mirrors them.
    """
    assert arabic.normalize(first) == arabic.normalize(second), label


def test_key_normalizer_keeps_the_letters_the_soft_one_folds():
    assert "ى" in arabic.normalize_key("المدعى")
    assert "ة" in arabic.normalize_key("محكمة")
    # And the soft one does not, which is the difference being relied on.
    assert "ى" not in arabic.normalize("المدعى")
    assert "ة" not in arabic.normalize("محكمة")


def test_key_normalizer_still_folds_what_cannot_carry_meaning():
    assert arabic.normalize_key("محـــكمة") == "محكمة"  # tatweel
    assert arabic.normalize_key("أَحْمَد") == "احمد"  # diacritics + hamza
    assert arabic.normalize_key("القضايا") == "قضايا"  # definite article


def test_a_question_with_no_content_has_no_key():
    """Punctuation alone must not hash to something lookupable."""
    assert query_cache.cache_key("؟؟؟") == ""
    assert query_cache.cache_key("") == ""


# --- Cache policy must not depend on the context knob -------------------------


def _one_turn():
    return [HistoryTurn(question="س", sql="SELECT 1", answer="ج", table_names=["cases"])]


@pytest.mark.parametrize("max_turns", [0, -1])
def test_non_positive_max_turns_still_reports_prior_turns(monkeypatch, max_turns):
    """The trap: trim_history returns [] for these, hiding a real transcript.

    run_pipeline derives cache policy from the untrimmed input, so what this
    pins is that the two are genuinely different values — a future refactor that
    goes back to reading the trimmed list would make this test's premise false.
    """
    monkeypatch.setattr(context, "CONTEXT_MAX_TURNS", max_turns)

    supplied = _one_turn()
    trimmed = context.trim_history(supplied)

    assert trimmed == []  # what the model would see
    assert bool(supplied) is True  # what cache policy must use


def test_positive_max_turns_keeps_the_recent_turns(monkeypatch):
    monkeypatch.setattr(context, "CONTEXT_MAX_TURNS", 1)
    assert len(context.trim_history(_one_turn() * 3)) == 1


# --- Read gating --------------------------------------------------------------


def test_follow_up_reads_are_off_by_default():
    """A chat with prior turns must not read another chat's cold-start SQL.

    Asserted on the config default rather than on behaviour because that default
    IS the fix: the read expression is `FOLLOW_UPS or not has_prior_turns`, so
    with it false the second clause is the whole gate.
    """
    from app.config import QUERY_CACHE_FOLLOW_UPS

    assert QUERY_CACHE_FOLLOW_UPS is False


def test_stored_entry_is_readable_by_key():
    """The cache still works for the case it was built for."""

    async def scenario():
        await query_cache.store("ما هي القضايا المدورة؟", "SELECT 1", ["cases"], [])
        # Same question, written the other way — the documented motivating case.
        return await query_cache.lookup("ما هي قضايا مدورة")

    entry = asyncio.run(scenario())
    assert entry is not None
    assert entry["sql"] == "SELECT 1"


def test_plaintiff_query_is_not_served_for_the_defendant_question():
    """The end-to-end shape of defect 1, at the cache boundary."""

    async def scenario():
        await query_cache.store(
            "من هو المدعي؟", "SELECT plaintiff FROM cases", ["cases"], []
        )
        return await query_cache.lookup("من هو المدعى؟")

    assert asyncio.run(scenario()) is None
