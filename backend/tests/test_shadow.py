"""Tests for plan-shape comparison and shadow verification.

Two properties matter here and they pull in opposite directions:

  1. A skeleton must be INSENSITIVE to the values a plan leaves open, or every
     shadowed hit diverges on the literal it just restored and no key is ever
     trusted.
  2. A skeleton must be SENSITIVE to every decision a plan makes, or the one
     mechanism that can catch an unanticipated wrong merge waves it through.

Most of the file is (2), because that is the direction that fails silently.

The shadow ledger is tested for one thing above all: it fails toward VERIFYING.
A ledger that cannot be read makes every key look unverified, which makes the
system slow. A ledger that wrongly reports a key as trusted makes it wrong.
"""

import json

import pytest

from app.services import plan_shape, shadow
from app.services.plan_shape import skeleton

COUNT_BY_YEAR = "SELECT COUNT(*) FROM cases WHERE YEAR(filed_at) = 2024"
COUNT_BY_YEAR_OTHER = "SELECT COUNT(*) FROM cases WHERE YEAR(filed_at) = 2023"


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "SHADOW_LEDGER_FILE", tmp_path / "shadow.jsonl")
    monkeypatch.setattr(shadow, "SHADOW_VERIFY_ENABLED", True)
    monkeypatch.setattr(shadow, "SHADOW_CONFIRMATIONS", 3)
    shadow.reset_for_tests()
    yield
    shadow.reset_for_tests()


# --- Skeletons ignore values ---------------------------------------------------


def test_the_same_plan_with_a_different_literal_matches():
    """If this fails, no key is ever trusted and the cache never engages."""
    assert plan_shape.compare(COUNT_BY_YEAR, COUNT_BY_YEAR_OTHER).matches


def test_arabic_string_literals_do_not_affect_the_skeleton():
    a = "SELECT id FROM cases WHERE title LIKE '%مدورة%'"
    b = "SELECT id FROM cases WHERE title LIKE '%مشطوبة%'"
    assert plan_shape.compare(a, b).matches


def test_whitespace_and_case_do_not_affect_the_skeleton():
    assert plan_shape.compare(
        "select count(*)  from   cases", "SELECT COUNT(*) FROM cases"
    ).matches


def test_a_comparison_operator_inside_a_string_is_not_read_as_an_operator():
    """The reason this module parses instead of regexing raw SQL."""
    quoted = "SELECT id FROM cases WHERE status = 'a > b'"
    plain = "SELECT id FROM cases WHERE status = 'x'"
    assert plan_shape.compare(quoted, plain).matches


def test_an_arabic_operator_word_inside_a_string_is_inert():
    a = "SELECT id FROM cases WHERE title LIKE '%قبل%'"
    b = "SELECT id FROM cases WHERE title LIKE '%بعد%'"
    assert plan_shape.compare(a, b).matches


# --- Skeletons catch every plan-level decision --------------------------------


def test_comparison_direction_is_part_of_the_plan():
    """The wrong-merge class this whole feature was rebuilt around."""
    before = "SELECT id FROM cases WHERE filed_at < '2020-01-01'"
    after = "SELECT id FROM cases WHERE filed_at > '2020-01-01'"

    comparison = plan_shape.compare(before, after)
    assert not comparison.matches
    assert any("comparators" in difference for difference in comparison.differences)


def test_strict_and_inclusive_comparisons_are_different_plans():
    assert not plan_shape.compare(
        "SELECT id FROM cases WHERE days > 100", "SELECT id FROM cases WHERE days >= 100"
    ).matches


def test_count_and_projection_are_different_plans():
    assert not plan_shape.compare(
        "SELECT COUNT(*) FROM cases", "SELECT id, title FROM cases"
    ).matches


def test_order_direction_is_part_of_the_plan():
    assert not plan_shape.compare(
        "SELECT id FROM cases ORDER BY filed_at DESC",
        "SELECT id FROM cases ORDER BY filed_at ASC",
    ).matches


def test_an_implicit_ascending_order_equals_an_explicit_one():
    assert plan_shape.compare(
        "SELECT id FROM cases ORDER BY filed_at", "SELECT id FROM cases ORDER BY filed_at ASC"
    ).matches


def test_a_different_table_is_a_different_plan():
    assert not plan_shape.compare(
        "SELECT COUNT(*) FROM cases", "SELECT COUNT(*) FROM hearings"
    ).matches


def test_an_extra_join_is_a_different_plan():
    assert not plan_shape.compare(
        "SELECT c.id FROM cases c JOIN parties p ON p.case_id = c.id",
        "SELECT c.id FROM cases c",
    ).matches


def test_negation_is_part_of_the_plan():
    assert not plan_shape.compare(
        "SELECT id FROM cases WHERE status NOT IN ('open')",
        "SELECT id FROM cases WHERE status IN ('open')",
    ).matches


def test_between_is_part_of_the_plan():
    assert not plan_shape.compare(
        "SELECT id FROM cases WHERE YEAR(filed_at) BETWEEN 2020 AND 2024",
        "SELECT id FROM cases WHERE YEAR(filed_at) = 2020",
    ).matches


def test_grouping_and_limiting_are_part_of_the_plan():
    assert not plan_shape.compare(
        "SELECT judge_id, COUNT(*) FROM cases GROUP BY judge_id",
        "SELECT judge_id, COUNT(*) FROM cases",
    ).matches
    assert not plan_shape.compare(
        "SELECT id FROM cases LIMIT 1", "SELECT id FROM cases"
    ).matches


def test_unparseable_sql_matches_nothing():
    """Fails toward 'these differ', so garbage never confirms a key."""
    assert not plan_shape.compare("not sql at all ((", COUNT_BY_YEAR).matches


def test_differences_name_the_field_that_disagreed():
    diffs = skeleton("SELECT COUNT(*) FROM cases").differences(skeleton("SELECT id FROM cases"))
    assert any("aggregates" in difference for difference in diffs)


# --- The ledger ----------------------------------------------------------------


def test_an_unknown_key_must_be_verified():
    assert shadow.should_verify("newkey") is True
    assert shadow.is_quarantined("newkey") is False


def test_a_key_becomes_trusted_after_enough_agreements():
    for _ in range(3):
        observation = shadow.record("k1", "س", COUNT_BY_YEAR, COUNT_BY_YEAR_OTHER)
        assert observation.verdict == shadow.VERDICT_CONFIRMED

    assert shadow.should_verify("k1") is False
    assert shadow.state_for("k1").trusted is True


def test_one_disagreement_quarantines_the_key():
    """One strike, not a ratio.

    A key that produced a wrong plan once has shown it merges questions it
    should not; how often it also produced a right one says nothing about that.
    """
    shadow.record("k2", "س", COUNT_BY_YEAR, COUNT_BY_YEAR_OTHER)
    shadow.record("k2", "س", "SELECT id FROM cases WHERE days < 100",
                  "SELECT id FROM cases WHERE days > 100")

    assert shadow.is_quarantined("k2") is True
    assert shadow.should_verify("k2") is True
    assert shadow.state_for("k2").trusted is False


def test_a_quarantined_key_stays_quarantined_however_many_agreements_follow():
    shadow.record("k3", "س", "SELECT id FROM cases WHERE a < 1", "SELECT id FROM cases WHERE a > 1")
    for _ in range(5):
        shadow.record("k3", "س", COUNT_BY_YEAR, COUNT_BY_YEAR_OTHER)

    assert shadow.is_quarantined("k3") is True
    assert shadow.state_for("k3").trusted is False


def test_a_missing_operator_diverges_even_when_the_shape_matches():
    """The gap plan_shape alone cannot see.

    Two queries can have identical skeletons and still answer opposite
    questions if the adapter reused the template's comparator — the skeleton
    records THAT there is a comparison, and the operator tag records WHICH.
    """
    observation = shadow.record(
        "k4", "ما هي القضايا بعد 2020؟", COUNT_BY_YEAR, COUNT_BY_YEAR_OTHER,
        missing_operators=["gt"],
    )

    assert observation.verdict == shadow.VERDICT_DIVERGED
    assert shadow.is_quarantined("k4") is True
    assert shadow.state_for("k4").reason == shadow.REASON_OPERATOR


def test_confirmations_survive_a_restart():
    for _ in range(3):
        shadow.record("k5", "س", COUNT_BY_YEAR, COUNT_BY_YEAR_OTHER)

    shadow.reset_for_tests()  # a fresh process

    assert shadow.should_verify("k5") is False


def test_a_quarantine_survives_a_restart():
    """Without this, restarting the backend puts a proven-unsafe key back in."""
    shadow.record("k6", "س", "SELECT id FROM cases WHERE a < 1", "SELECT id FROM cases WHERE a > 1")

    shadow.reset_for_tests()

    assert shadow.is_quarantined("k6") is True


def test_a_corrupt_ledger_line_does_not_lose_the_rest(tmp_path, monkeypatch):
    path = tmp_path / "shadow.jsonl"
    good = {
        "ts": 1.0, "key": "k7", "question": "س", "verdict": shadow.VERDICT_DIVERGED,
        "adapted_sql": "", "reference_sql": "", "differences": [],
    }
    path.write_text("{ not json\n" + json.dumps(good) + "\n", encoding="utf-8")
    monkeypatch.setattr(shadow, "SHADOW_LEDGER_FILE", path)
    shadow.reset_for_tests()

    assert shadow.is_quarantined("k7") is True


def test_an_unwritable_ledger_still_answers_and_fails_toward_verifying(monkeypatch):
    """A broken ledger must make the system slow, never wrong."""
    monkeypatch.setattr(shadow, "SHADOW_LEDGER_FILE", "/nonexistent-dir/\x00/shadow.jsonl")
    shadow.reset_for_tests()

    observation = shadow.record("k8", "س", "SELECT id FROM cases WHERE a < 1",
                                "SELECT id FROM cases WHERE a > 1")

    assert observation.verdict == shadow.VERDICT_DIVERGED
    shadow.reset_for_tests()
    assert shadow.should_verify("k8") is True


def test_disabling_verification_stops_shadowing():
    from app.services import shadow as module

    module.SHADOW_VERIFY_ENABLED = False
    try:
        assert module.should_verify("anything") is False
    finally:
        module.SHADOW_VERIFY_ENABLED = True


def test_stats_and_listings_report_the_three_states():
    for _ in range(3):
        shadow.record("trusted", "س", COUNT_BY_YEAR, COUNT_BY_YEAR_OTHER)
    shadow.record("verifying", "س", COUNT_BY_YEAR, COUNT_BY_YEAR_OTHER)
    shadow.record("bad", "س", "SELECT id FROM cases WHERE a < 1", "SELECT id FROM cases WHERE a > 1")

    stats = shadow.stats()
    assert stats["trusted"] == 1
    assert stats["verifying"] == 1
    assert stats["quarantined"] == 1
    assert [entry["key"] for entry in shadow.quarantined_keys()] == ["bad"]
    assert len(shadow.recent(verdict=shadow.VERDICT_DIVERGED)) == 1
