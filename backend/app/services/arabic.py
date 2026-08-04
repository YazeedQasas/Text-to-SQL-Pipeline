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

_ALEF_VARIANTS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه"})

# The definite article, at the start of any word. "التشريعات السارية" must reach
# the concept written "التشريع الساري", and stripping ال is what lets the shared
# stem line up.
_DEFINITE_ARTICLE = re.compile(r"\bال")


def normalize(text: str) -> str:
    """Fold spelling variation the way the database's *_norm columns do.

    Diacritics dropped, hamza forms collapsed to bare alef, ى to ي, ة to ه, the
    definite article removed, whitespace collapsed. Deliberately matches the
    normalization described in schema_docs.py so a term written either way in a
    question reaches the same concept.
    """
    folded = unicodedata.normalize("NFC", text)
    folded = _DIACRITICS.sub("", folded)
    folded = folded.translate(_ALEF_VARIANTS)
    folded = _DEFINITE_ARTICLE.sub("", folded)
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
