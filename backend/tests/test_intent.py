"""Tests for coarse intent extraction — what may share a cache key under APC.

test_cache_keying.py pins the OLD rule: a key folds only what cannot change
meaning. This file pins the new one, which deliberately breaks that rule and is
safe only because of what replaces it:

    a specific may be dropped from the KEY, but never from the REQUEST.

So the assertions come in pairs throughout. Every test that shows two questions
collapsing onto one key is followed by one showing the thing that distinguished
them still present in `specifics`, where the adapter can put it back. A test
that only checked the collision would be checking the dangerous half.

The rails are tested against ADVERSARIAL model output on purpose — a leaked year
in the keyword, a role the model forgot to tag, junk instead of JSON. The
guarantee this module owes is mechanical, so it has to hold when the model does
the worst plausible thing rather than the documented thing.

Live extraction against real Gemma is in test_intent_live.py, which is skipped
unless LM Studio is up. This file never touches the network.
"""

import asyncio
import json

import pytest

from app.services import arabic, intent, llm
from app.services.intent import Specific


def _payload(keyword: str, specifics=()) -> dict:
    return {
        "intent_keyword": keyword,
        "specifics": [{"tag": tag, "value": value} for tag, value in specifics],
    }


def _key(question: str, payload: dict) -> str:
    """The cache key a question would get from this model response."""
    return intent.keyword_hash(intent.build_intent(question, payload).keyword)


# --- Rail 1: nothing specific can reach a keyword ------------------------------


def test_digits_are_deleted_from_a_keyword():
    """The rail that makes 2023/2024 collide without trusting the model."""
    assert intent.canonical_keyword("case count by year 2024") == "case count year"
    assert intent.canonical_keyword("2023") == ""


def test_arabic_is_deleted_from_a_keyword():
    """A model answering in Arabic must not smuggle a party name into the key."""
    assert intent.canonical_keyword("party lookup المدعي") == "party list"
    assert intent.canonical_keyword("بحث عن المدعي") == ""


def test_case_and_separators_are_folded():
    assert intent.canonical_keyword("Case_Count-By Year") == "case count year"
    assert intent.canonical_keyword("  case   count  ") == "case count"


# --- Rail 1b: synonym drift is folded, because it was the measured failure ----

# Every one of these is a real pair of Gemma outputs for two questions of the
# same shape. Before folding they produced different keys, so the two questions
# never shared a plan and the feature silently did nothing for them.
DRIFT_PAIRS = [
    ("case list by year", "case listing by year"),
    ("case lookup by role", "case list by role"),
    ("case listing by party role", "case lookup by party role"),
    ("case list for judge", "case listing by judge"),
    ("party lookup by role", "party list by role"),
]


@pytest.mark.parametrize(("first", "second"), DRIFT_PAIRS)
def test_synonym_drift_folds_to_one_keyword(first, second):
    assert intent.canonical_keyword(first) == intent.canonical_keyword(second)
    assert intent.canonical_keyword(first) != ""


def test_folding_keeps_genuinely_different_intents_apart():
    """The folding must not buy collisions by merging questions that differ.

    A list and a count over the same subject need different SQL, and so do the
    same question asked about a different dimension.
    """
    distinct = [
        ("case list by year", "case count by year"),
        ("case count by court", "case count by judge"),
        ("case count by year", "hearing count by year"),
        ("party lookup by role", "judge lookup by case number"),
    ]
    for first, second in distinct:
        assert intent.canonical_keyword(first) != intent.canonical_keyword(second), (
            first,
            second,
        )


def test_plural_folding_does_not_maul_words_ending_in_s():
    assert intent.canonical_keyword("case status") == "case status"
    assert intent.canonical_keyword("court list") == intent.canonical_keyword("courts lists")


def test_an_overlong_keyword_is_rejected_rather_than_truncated():
    """Truncation would merge distinct intents, which is the failure to avoid.

    "case count by court" and "case count by judge" share a three-word prefix
    and need completely different plans.
    """
    assert intent.canonical_keyword("how many cases were filed in the court last year") == ""


def test_a_keyword_with_no_letters_is_unusable():
    assert intent.canonical_keyword("") == ""
    assert intent.canonical_keyword("2024 ؟ -- 12") == ""
    assert intent.canonical_keyword("a") == ""


def test_hash_is_stable_and_empty_for_no_keyword():
    assert intent.keyword_hash("case count by year") == intent.keyword_hash("case count by year")
    assert intent.keyword_hash("") == ""


# --- The headline property: year variants collide, specifics do not -----------

YEAR_PAIRS = [
    (
        "كم عدد القضايا المسجلة في 2023؟",
        "كم عدد القضايا المسجلة في 2024؟",
        "case count by year",
        "registered case count",
    ),
    (
        "كم جلسة عُقدت في 2022؟",
        "كم جلسة عُقدت في 2025؟",
        "hearing count by year",
        "hearing count by year",
    ),
]


@pytest.mark.parametrize(("first", "second", "kw_a", "kw_b"), YEAR_PAIRS)
def test_year_variants_share_a_key(first, second, kw_a, kw_b):
    """Same shape, different year — one plan template between them."""
    del kw_b  # both sides use the same keyword here; the leak case is below
    a = intent.build_intent(first, _payload(kw_a, [("year", "2023")]))
    b = intent.build_intent(second, _payload(kw_a, [("year", "2024")]))

    assert a.keyword == b.keyword
    assert intent.keyword_hash(a.keyword) == intent.keyword_hash(b.keyword)


@pytest.mark.parametrize(("first", "second", "kw_a", "kw_b"), YEAR_PAIRS)
def test_year_variants_keep_different_specifics(first, second, kw_a, kw_b):
    """The other half. A shared key is only safe because this holds."""
    del kw_b
    a = intent.build_intent(first, _payload(kw_a, [("year", "2023")]))
    b = intent.build_intent(second, _payload(kw_a, [("year", "2024")]))

    assert a.specifics != b.specifics
    assert a.values_by_tag("year") == ["2023"]
    assert b.values_by_tag("year") == ["2024"]


def test_a_year_leaked_into_the_keyword_still_collides():
    """The adversarial case: the model ignores the instruction and keys the year.

    Without rail 1 this is the whole mechanic failing silently — every year gets
    its own key, the cache never shares a plan, and nobody notices because the
    answers are all correct.
    """
    first = "كم عدد القضايا في 2023؟"
    second = "كم عدد القضايا في 2024؟"

    a = intent.build_intent(first, _payload("case count in 2023", [("year", "2023")]))
    b = intent.build_intent(second, _payload("case count in 2024", [("year", "2024")]))

    assert a.keyword == b.keyword == "case count"
    assert a.values_by_tag("year") != b.values_by_tag("year")


def test_different_shapes_do_not_collide():
    """Collision is for same-shape questions only, not for everything."""
    counting = _key("كم عدد القضايا في 2024؟", _payload("case count by year", [("year", "2024")]))
    lookup = _key(
        "من هو قاضي القضية MS-2026-0301؟",
        _payload("judge lookup for case", [("case_number", "MS-2026-0301")]),
    )

    assert counting != lookup


# --- Rail 2: the literal backstop ---------------------------------------------


def test_an_untagged_year_is_recovered():
    """The model tagged nothing; the year must still reach the adapter."""
    result = intent.build_intent("كم عدد القضايا في 2024؟", _payload("case count by year"))

    assert result.cacheable
    assert Specific(intent.TAG_LITERAL, "2024") in result.specifics


def test_an_untagged_case_number_is_recovered():
    result = intent.build_intent(
        "ما هي جلسات القضية MS-2026-0301؟", _payload("hearing list for case")
    )

    assert Specific(intent.TAG_LITERAL, "MS-2026-0301") in result.specifics


def test_a_tagged_value_is_not_duplicated_by_the_backstop():
    result = intent.build_intent(
        "كم عدد القضايا في 2024؟", _payload("case count by year", [("year", "2024")])
    )

    assert [s.value for s in result.specifics] == ["2024"]


def test_a_year_inside_a_tagged_code_is_not_split_out():
    """Restoring MS-2026-0301 restores its 2026; a bare "2026" specific would
    invite the adapter to filter on a year the question never asked about."""
    result = intent.build_intent(
        "ما هي جلسات القضية MS-2026-0301؟",
        _payload("hearing list for case", [("case_number", "MS-2026-0301")]),
    )

    assert [s.value for s in result.specifics] == ["MS-2026-0301"]


def test_a_question_with_no_specifics_is_still_cacheable():
    """Most questions have none. That is normal, not a failure."""
    result = intent.build_intent("ما هي المحاكم الموجودة؟", _payload("court list"))

    assert result.cacheable
    assert result.specifics == []


# --- Rail 3: the المدعي / المدعى class ----------------------------------------

# The pair query_cache.py was rewritten for. Opposite parties, one letter apart,
# no digits — no regex can tell them apart, so the model must tag the role.
ROLE_PAIRS = [
    ("من هو المدعي؟", "من هو المدعى؟", "المدعي", "المدعى"),
    ("قضايا المدعي", "قضايا المدعى", "المدعي", "المدعى"),
    ("ما هي قضايا المستدعي؟", "ما هي قضايا المستدعى ضده؟", "المستدعي", "المستدعى ضده"),
]


@pytest.mark.parametrize(("first", "second", "role_a", "role_b"), ROLE_PAIRS)
def test_role_pairs_share_a_key_when_the_role_is_tagged(first, second, role_a, role_b):
    a = intent.build_intent(first, _payload("party lookup by role", [("role", role_a)]))
    b = intent.build_intent(second, _payload("party lookup by role", [("role", role_b)]))

    assert a.cacheable and b.cacheable
    assert intent.keyword_hash(a.keyword) == intent.keyword_hash(b.keyword)


@pytest.mark.parametrize(("first", "second", "role_a", "role_b"), ROLE_PAIRS)
def test_role_pairs_keep_the_role_that_distinguishes_them(first, second, role_a, role_b):
    """Sharing a key is only survivable because the role travels with it."""
    a = intent.build_intent(first, _payload("party lookup by role", [("role", role_a)]))
    b = intent.build_intent(second, _payload("party lookup by role", [("role", role_b)]))

    assert a.values_by_tag("role") == [role_a]
    assert b.values_by_tag("role") == [role_b]
    assert a.specifics != b.specifics


@pytest.mark.parametrize(("first", "second", "role_a", "role_b"), ROLE_PAIRS)
def test_an_untagged_role_makes_the_question_uncacheable(first, second, role_a, role_b):
    """The one case with no mechanical recovery: refuse the key entirely.

    If this ever regresses to `cacheable`, the plaintiff's plan can be served
    for a question about the defendant — the exact defect query_cache.py was
    rewritten to close, reintroduced one layer up.
    """
    del role_a, role_b
    for question in (first, second):
        result = intent.build_intent(question, _payload("party lookup by role"))
        assert not result.cacheable, question
        assert "untagged party role" in result.reason


def test_role_detection_uses_the_strict_normalizer():
    """normalize() folds ى→ي and would report both roles for either question."""
    assert intent.role_terms_in("من هو المدعي؟") == ["المدعي"]
    assert "المدعى" not in intent.role_terms_in("من هو المدعي؟")
    assert "المدعي" not in intent.role_terms_in("من هو المدعى؟")


def test_a_role_tagged_with_the_other_sides_word_does_not_count_as_covered():
    """Tagging *a* role is not enough; it has to be the one that was asked."""
    result = intent.build_intent(
        "من هو المدعى؟", _payload("party lookup by role", [("role", "المدعي")])
    )

    assert not result.cacheable


def test_a_question_naming_no_role_is_unaffected():
    assert intent.role_terms_in("كم عدد القضايا في 2024؟") == []


def test_role_surfaces_still_cover_the_glossarys_party_concepts():
    """Drift alarm against data/concepts.json.

    The role rail is a hardcoded list because it must run without Qdrant. That
    is only safe while it keeps up with the glossary, so this fails when a party
    concept grows a surface form the rail has not been told about.
    """
    from app.config import CONCEPTS_FILE

    concepts = json.loads(CONCEPTS_FILE.read_text(encoding="utf-8"))["concepts"]
    party_concepts = [c for c in concepts if c.get("id") in {"mustadei", "mustadaa_diddahu"}]
    assert party_concepts, "the glossary no longer has the party-role concepts"

    known = {arabic.normalize_key(s) for s in intent._ROLE_SURFACES}
    for concept in party_concepts:
        for surface in [concept["term"], *concept.get("aliases", [])]:
            folded = arabic.normalize_key(surface)
            assert any(k in folded or folded in k for k in known), (
                f"glossary surface {surface!r} of {concept['id']!r} is not covered by "
                "_ROLE_SURFACES — a question using it could be cached without its role"
            )


# --- Specifics parsing tolerates what small models actually emit ---------------


def test_documented_shape_parses():
    assert intent.parse_specifics([{"tag": "year", "value": "2024"}]) == [Specific("year", "2024")]


def test_object_keyed_by_tag_parses():
    assert intent.parse_specifics({"year": "2024", "role": "المدعي"}) == [
        Specific("year", "2024"),
        Specific("role", "المدعي"),
    ]


def test_a_tag_holding_several_values_parses():
    assert intent.parse_specifics({"year": ["2023", "2024"]}) == [
        Specific("year", "2023"),
        Specific("year", "2024"),
    ]


def test_bare_values_are_kept_under_a_fallback_tag():
    """The value is what the adapter needs; losing it over a missing tag is worse."""
    assert intent.parse_specifics(["2024"]) == [Specific("other", "2024")]


def test_an_unknown_tag_is_kept_not_dropped():
    assert intent.parse_specifics([{"tag": "invented", "value": "x"}]) == [Specific("invented", "x")]


def test_junk_specifics_yield_nothing_rather_than_raising():
    assert intent.parse_specifics(None) == []
    assert intent.parse_specifics("2024") == []
    assert intent.parse_specifics([{"tag": "year"}]) == []


def test_numbers_are_stringified():
    assert intent.parse_specifics([{"tag": "year", "value": 2024}]) == [Specific("year", "2024")]


def test_sentence_punctuation_is_stripped_from_a_value():
    """Observed on real Gemma output, not hypothetical.

    Asked for the value verbatim it returned "المستدعى ضده؟" and "محكمة البداية؟".
    The plan is for sql_guard to check each specific reached the generated SQL,
    and no SQL will ever contain a ؟ — so the trailing mark would fail every
    such check on questions that are otherwise perfectly handled.
    """
    assert intent.parse_specifics([{"tag": "role", "value": "المستدعى ضده؟"}]) == [
        Specific("role", "المستدعى ضده")
    ]
    assert intent.parse_specifics([{"tag": "court", "value": "محكمة البداية؟"}]) == [
        Specific("court", "محكمة البداية")
    ]


def test_stripping_leaves_the_inside_of_a_reference_code_alone():
    """Edges only — a case number is mostly separators and must survive intact."""
    assert intent.parse_specifics([{"tag": "case_number", "value": "MS-2026-0301"}]) == [
        Specific("case_number", "MS-2026-0301")
    ]


def test_a_value_that_is_only_punctuation_is_dropped():
    assert intent.parse_specifics([{"tag": "other", "value": "؟"}]) == []


def test_punctuation_stripping_makes_the_role_rail_pass(monkeypatch):
    """The two bugs interact: an unstripped ؟ still covers its role surface.

    Pinned because the covering check uses containment, so this would keep
    working by luck if stripping regressed — and then fail in stage 4 instead,
    where it looks like an adapter fault.
    """
    del monkeypatch
    result = intent.build_intent(
        "ما هي قضايا المستدعى ضده؟",
        _payload("case list by party role", [("role", "المستدعى ضده؟")]),
    )

    assert result.cacheable
    assert result.values_by_tag("role") == ["المستدعى ضده"]


# --- extract() degrades to a miss, never to an exception ----------------------


def test_transport_failure_is_a_miss_not_an_error(monkeypatch):
    async def down(_system, _user):
        raise ConnectionError("LM Studio is not running")

    monkeypatch.setattr(llm, "chat_json", down)

    result = asyncio.run(intent.extract("كم عدد القضايا في 2024؟"))

    assert not result.cacheable
    assert result.source == "failed"
    # The trace still records what was in the question.
    assert Specific(intent.TAG_LITERAL, "2024") in result.specifics


def test_extraction_retries_once_before_giving_up(monkeypatch):
    calls = {"n": 0}

    async def flaky(_system, _user):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("No JSON object found in model response.")
        return _payload("case count by year", [("year", "2024")])

    monkeypatch.setattr(llm, "chat_json", flaky)

    result = asyncio.run(intent.extract("كم عدد القضايا في 2024؟"))

    assert calls["n"] == 2
    assert result.cacheable
    assert result.keyword == "case count year"


def test_an_empty_question_never_gets_a_key():
    assert not asyncio.run(intent.extract("   ")).cacheable


def test_a_keyword_made_only_of_specifics_is_unusable(monkeypatch):
    """Scrubbing can empty a keyword. That is a miss, not a key of "".

    Guards against the worst possible bug in this module: every question whose
    keyword scrubs away sharing one key, and therefore one plan.
    """
    result = intent.build_intent(
        "من هو المدعي؟", _payload("plaintiff", [("role", "plaintiff")])
    )

    assert not result.cacheable
    assert intent.keyword_hash(result.keyword) == ""


def test_scrubbing_removes_a_latin_specific_from_the_keyword():
    result = intent.build_intent(
        "ما هي جلسات القضية MS-2026-0301؟",
        _payload("ms hearing list", [("case_number", "MS-2026-0301")]),
    )

    assert result.keyword == "hearing list"
