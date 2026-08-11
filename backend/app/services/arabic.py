"""Arabic text normalization and n-gram extraction for concept matching.

Pure functions, no I/O — the matching policy lives here so it can be tested
without Qdrant or an embedding server.

Why n-grams at all
------------------
A concept is matched against the user's question, and matching the WHOLE
question was measurably unreliable: the same concept scored 0.6157 against
"كم عدد القضايا المدورة؟" and only 0.5046 against "أي محكمة لديها أكبر عدد من
القضايا المدورة؟". The term is identical; the extra words dilute it, and the
second question fell below the threshold and silently produced wrong SQL.

Scoring each fragment separately and keeping the best removes that dilution —
the same concept scores 0.6228 on the long question. Question length stops
mattering, which was the actual defect.

Bigrams are not optional. Almost every Palestinian legal term here is two
words ("محاكم الصلح", "القضايا المدورة", "المستدعى ضده"), and in the probe runs
the winning fragment was a bigram nearly every time. Unigrams alone would split
the terms this module exists to find.
"""

import re
import unicodedata

# Function words and question scaffolding. These are stripped from unigrams and,
# more importantly, disqualify a BIGRAM that contains one: fragments like
# "ما هو" and "الجلسات التي" were scoring against unrelated concepts purely on
# sentence shape. Filtering them dropped the weather question's best score from
# 0.4168 to 0.3358.
STOPWORDS = frozenset(
    """
    ما هي هو من في على كم أي عدد التي الذي هذا هذه هؤلاء لدينا هل عن إلى مع و أو
    لماذا متى أين كيف كانت كان تم قد لقد ثم أن إن لا نعم كل بعض غير سوى حتى منذ
    بين أمام خلف تحت فوق بعد قبل عند لدى هناك هنالك اليوم أمس غدا الآن أعطني
    أظهر اعرض أريد نريد يوجد توجد الرجاء أكبر أصغر أكثر أقل لديها لديه به بها
    """.split()
)

# Arabic diacritics (harakat), Quranic annotation marks, and tatweel.
_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")

# Letters and digits only. NOTE: Arabic punctuation lives INSIDE the Arabic
# Unicode block — ؟ is U+061F, ، is U+060C, ؛ is U+061B — so a naive
# [؀-ۿ] class keeps them attached to words and produced fragments like
# "القضايا المدورة؟" that scored measurably worse than the clean term.
_WORD = re.compile(r"[A-Za-z0-9ء-غف-ي٠-٩]+")

# Folding is split in two, because the two halves have different risk and the
# callers have different tolerance for it.
#
# SAFE: no two distinct Arabic words differ only by which hamza-carrier is
# written. Omitting the hamza is one of the most common spelling variations
# there is, so folding these buys recall and costs nothing.
_HAMZA_FORMS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"})

# LOSSY: these two MERGE DISTINCT WORDS, and in this domain they merge words
# that name opposite parties to a case. Measured:
#
#   ى → ي     المدعي (plaintiff)      ≡ المدعى (the claimed/sued)
#             على (preposition "on")  ≡ علي (the given name)
#   ة → ه     كتابة العقد (drafting)  ≡ كتابه العقد (his book)
#             محاكمة المتهم (trial)   ≡ محاكمه (his court)
#
# They are still applied for SEARCH, where a wrong match is one candidate among
# several that a threshold and a score can still reject, and where they mirror
# the database's *_norm columns (db/01_schema.sql) so a term written either way
# reaches the same row. They are NOT applied to a cache key — see normalize_key.
_LOSSY_FORMS = str.maketrans({"ى": "ي", "ة": "ه"})

# The definite article, at the start of any word. "التشريعات السارية" must reach
# the concept written "التشريع الساري", and stripping ال is what lets the shared
# stem line up.
_DEFINITE_ARTICLE = re.compile(r"\bال")


def _fold_common(text: str) -> str:
    """The folds both normalizers share: NFC, diacritics, tatweel, hamza forms."""
    folded = unicodedata.normalize("NFC", text)
    folded = _DIACRITICS.sub("", folded)
    return folded.translate(_HAMZA_FORMS)


def normalize(text: str) -> str:
    """Fold spelling variation the way the database's *_norm columns do.

    THE SOFT NORMALIZER, for search and concept matching. Diacritics dropped,
    hamza forms collapsed to bare alef, ى to ي, ة to ه, the definite article
    removed, whitespace collapsed. Deliberately matches the normalization
    described in schema_docs.py so a term written either way in a question
    reaches the same concept.

    Recall is the right trade here: a concept that matches too broadly still has
    to clear CONCEPT_SCORE_THRESHOLD, and it competes with other candidates. Do
    not use this to key a cache — see normalize_key for why.
    """
    folded = _fold_common(text).translate(_LOSSY_FORMS)
    folded = _DEFINITE_ARTICLE.sub("", folded)
    return re.sub(r"\s+", " ", folded).strip()


def normalize_key(text: str) -> str:
    """Fold ONLY what cannot change meaning. For cache keys.

    THE STRICT NORMALIZER. Identical to normalize() except that it does not
    apply _LOSSY_FORMS, because those merge words this domain has to keep apart
    — "من هو المدعي؟" and "من هو المدعى؟" hashed to the same key, and that key
    decides which SQL runs.

    The two normalizers exist because the two callers fail differently, and the
    asymmetry is the whole argument:

        a false MERGE in search   → one weak candidate, rejected by a threshold
        a false MERGE in a key    → the wrong stored query runs, and reports a
                                    confident number for a question nobody asked
        a false SPLIT in a key    → a cache miss; the question is answered from
                                    scratch, correctly, a few seconds slower

    So search folds aggressively and a key folds conservatively. The cost is
    real and accepted: "شكوى" and "شكوي" are one word spelled two ways and now
    key differently, which is a hit we give up to stop serving plaintiff's SQL
    for a question about the defendant.

    What is still folded is what genuinely cannot carry meaning: Unicode
    canonicalisation, diacritics and tatweel (decoration in modern prose), the
    hamza carriers, and the definite article — the last of which is the case
    this cache was built for, "القضايا المدورة" reaching "قضايا مدورة".
    """
    folded = _DEFINITE_ARTICLE.sub("", _fold_common(text))
    return re.sub(r"\s+", " ", folded).strip()


def words(text: str) -> list[str]:
    """Split into letter/digit runs, discarding punctuation of either script."""
    return _WORD.findall(text)


def content_grams(question: str) -> list[str]:
    """Fragments of `question` worth scoring against the glossary on their own.

    Content unigrams plus bigrams whose BOTH halves are content words. Short
    unigrams are dropped because a three-letter Arabic word carries too little
    signal to match a concept on, and matching on it produces noise rather than
    recall.

    Returns fragments in their original surface form, not normalized: they are
    handed to the embedding model, which was trained on ordinary text. The
    normalized form is for `lexical` matching only.
    """
    tokens = words(question)
    unigrams = [w for w in tokens if len(w) > 3 and w not in STOPWORDS]
    bigrams = [
        f"{tokens[i]} {tokens[i + 1]}"
        for i in range(len(tokens) - 1)
        if tokens[i] not in STOPWORDS and tokens[i + 1] not in STOPWORDS
    ]
    # Deduplicate while preserving order, so a repeated word is embedded once.
    seen: set[str] = set()
    return [g for g in unigrams + bigrams if not (g in seen or seen.add(g))]


# A surface form shorter than this is not specific enough to match on: after
# normalization, short strings appear inside unrelated words.
_MIN_LEXICAL_LENGTH = 5


def is_subsumed(surface: str, others: list[str]) -> bool:
    """True if `surface` is contained in a strictly longer one of `others`.

    Used to drop the less specific of two concepts that matched overlapping text.
    Equal-length forms never subsume each other, so two distinct terms that
    normalize identically both survive rather than one silently winning.
    """
    normalized = normalize(surface)
    return any(
        normalized in normalize(other) and len(normalize(other)) > len(normalized)
        for other in others
    )


def lexical_match(question: str, surfaces: list[str]) -> str | None:
    """Return the longest of `surfaces` that literally appears in `question`.

    Both sides are normalized first, so "التشريعات" can reach "التشريع".
    Longest-match-wins because concept surface forms nest: "المستدعي" is a
    substring of "المستدعى ضده" once normalized, and the more specific term is
    the one the user meant.
    """
    normalized_question = normalize(question)
    for surface in sorted(surfaces, key=len, reverse=True):
        normalized = normalize(surface)
        if len(normalized) >= _MIN_LEXICAL_LENGTH and normalized in normalized_question:
            return surface
    return None
