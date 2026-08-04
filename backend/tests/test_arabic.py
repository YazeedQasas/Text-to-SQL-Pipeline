"""Tests for Arabic normalization and question-fragment extraction.

These are the matching policy for the domain glossary, expressed as pure
functions so the rules can be pinned without Qdrant or an embedding server.
Each test names the real failure it guards against — every one of them comes
from a measured miss, not from imagination.
"""

import pytest

from app.services import arabic


# --- normalization -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Hamza forms collapse to bare alef, as the database's *_norm columns do.
        ("إيمان", "ايمان"),  # إ -> ا; no definite article here to strip
        ("مُحَاكِم الصُّلح", "محاكم صلح"),  # diacritics dropped, article stripped
        ("القضايا المدورة", "قضايا مدوره"),  # ة -> ه on both words
        ("المستدعى ضده", "مستدعي ضده"),  # ى -> ي
        ("التشريعات   السارية", "تشريعات ساريه"),  # whitespace collapsed
    ],
)
def test_normalize_folds_the_variation_that_blocks_matching(raw, expected):
    assert arabic.normalize(raw) == expected


def test_normalize_strips_tatweel():
    assert arabic.normalize("محـــكمة") == "محكمه"


# --- fragment extraction -----------------------------------------------------


def test_arabic_question_marks_do_not_stay_glued_to_words():
    """؟ is U+061F — INSIDE the Arabic block, so a naive range keeps it.

    It did, and "القضايا المدورة؟" scored measurably worse than the clean term.
    """
    grams = arabic.content_grams("ما هي القضايا المدورة؟")

    assert "القضايا المدورة" in grams
    assert not any("؟" in g for g in grams)


def test_bigrams_are_produced_because_the_terms_are_two_words():
    """Unigrams alone would split every term this module exists to find."""
    grams = arabic.content_grams("كم قضية مفتوحة أمام محاكم الصلح")

    assert "محاكم الصلح" in grams


def test_a_bigram_containing_a_stopword_is_not_offered():
    """Fragments like "ما هو" scored against unrelated concepts on sentence shape."""
    grams = arabic.content_grams("ما هو الطقس اليوم")

    assert "ما هو" not in grams
    assert not any(w in g.split() for g in grams for w in ("ما", "هو"))


def test_short_words_are_dropped_as_too_unspecific():
    grams = arabic.content_grams("من هو وكيل المستدعى ضده")

    assert "ضده" not in grams  # three letters, too little signal on its own
    assert "المستدعى ضده" in grams  # but it survives inside the bigram


def test_fragments_are_deduplicated():
    grams = arabic.content_grams("القضايا المدورة والقضايا المدورة")

    assert len(grams) == len(set(grams))


def test_fragments_keep_their_original_spelling():
    """They go to the embedding model, which was trained on ordinary text."""
    assert "المدورة" in arabic.content_grams("ما هي القضايا المدورة؟")


# --- lexical matching --------------------------------------------------------


def test_a_term_written_in_the_question_is_found():
    assert (
        arabic.lexical_match("أي محكمة لديها أكبر عدد من القضايا المدورة؟", ["القضايا المدورة"])
        == "القضايا المدورة"
    )


def test_matching_sees_through_the_definite_article():
    assert arabic.lexical_match("كم قضية أمام محاكم صلح؟", ["محاكم الصلح"]) == "محاكم الصلح"


def test_the_longest_surface_form_wins():
    """Concept surface forms nest; the specific one is what the user meant."""
    hit = arabic.lexical_match("من هو وكيل المستدعى ضده؟", ["المستدعي", "المستدعى ضده"])

    assert hit == "المستدعى ضده"


def test_a_plural_does_not_reach_its_singular_term():
    """The known gap the vector signal exists to cover — not a bug, a boundary."""
    assert arabic.lexical_match("ما التشريعات السارية؟", ["التشريع الساري"]) is None


def test_very_short_surfaces_are_never_matched_on():
    """After normalization a short string turns up inside unrelated words."""
    assert arabic.lexical_match("ما هي القضايا المدنية؟", ["حكم"]) is None


# --- subsumption -------------------------------------------------------------


def test_a_shorter_overlapping_term_is_subsumed():
    """المستدعي inside المستدعى ضده — opposite party roles, not near-synonyms."""
    assert arabic.is_subsumed("المستدعي", ["المستدعى ضده"]) is True


def test_the_longer_term_is_not_subsumed_by_the_shorter():
    assert arabic.is_subsumed("المستدعى ضده", ["المستدعي"]) is False


def test_unrelated_terms_do_not_subsume_each_other():
    assert arabic.is_subsumed("محكمة الصلح", ["القضايا المدورة"]) is False


def test_forms_that_normalize_identically_both_survive():
    """Equal length cannot subsume, so neither silently wins over the other."""
    assert arabic.is_subsumed("القضية", ["القضيه"]) is False
