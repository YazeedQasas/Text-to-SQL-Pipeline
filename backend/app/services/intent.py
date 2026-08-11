"""Coarse intent extraction: the cache key, and the specifics it deliberately drops.

This is stage one of Agentic Plan Caching. The repeated-question cache keys on
the whole normalized question (services/query_cache.py), so only a re-ask of the
*same* question hits. APC keys on the SHAPE of the question instead — "how many
cases in <year>" rather than "how many cases in 2024" — so questions of one
shape share one stored plan, and a small adapter model rebuilds the exact SQL
from the specifics that the key threw away.

WHY THIS MODULE IS THE DANGEROUS ONE
------------------------------------
query_cache.py argues at length that a key may fold only what cannot change
meaning, because a false merge runs the wrong stored query and reports a
confident number for a question nobody asked. That argument has not been
retracted. This module folds far more than the normalizer ever did — 2023 and
2024 land on one key by design — and it is safe only under one condition:

    NOTHING DROPPED FROM THE KEY MAY BE LOST FROM THE REQUEST.

A specific removed from the key must still travel to the adapter, or the SQL it
writes cannot be right. So the guarantee this module owes its callers is not
"the keyword is good", it is "every discriminating token in the question is
either in the keyword or in `specifics`". Two mechanical rails enforce it, and
neither trusts the model:

  1. ASCII-ONLY, DIGIT-FREE KEYWORDS (canonical_keyword). Digits and Arabic are
     stripped from the keyword outright, so a year or an Arabic party name
     CANNOT reach the key even if the model puts one there. This is what makes
     the 2023/2024 collision true by construction rather than by prompt
     compliance.
  2. THE LITERAL BACKSTOP (_literal_backstop). Every year, reference code and
     multi-digit number in the question is matched by regex and appended to
     `specifics` if the model failed to tag it. Rail 1 guarantees such a token
     is not in the key; this guarantees it is still in the request.

Together: a digit-bearing token is never in the key and always in the specifics.
That property is a regex and a character class, not a model behaviour.

WHAT THE RAILS CANNOT COVER
---------------------------
Arabic minimal pairs. "من هو المدعي؟" (plaintiff) and "من هو المدعى؟" (the sued)
differ by one letter, carry no digits, and name opposite parties — this is the
exact pair query_cache.py was rewritten for. No regex tells them apart from
ordinary words, so the model has to tag the role, and if it does not, the
question must not be cached at all.

_ROLE_SURFACES plus _role_terms_covered are that rule: when the question names a
party role and no specific carries it, extraction returns an unusable keyword and
the pipeline runs from scratch. Correct and slow beats fast and wrong, and it is
the same fail-toward-safety choice the reviewer makes for unparseable verdicts.

FAILURE POLICY
--------------
Every failure here degrades to "no key", which degrades to a cache miss, which
is the behaviour the system had before APC existed. Nothing in this module may
raise into the pipeline, and nothing in it may return a key it is not sure of.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field

from app.services import arabic, llm

logger = logging.getLogger(__name__)

# Two attempts, matching services/reviewer.py: small instruct models drop a
# closing brace often enough that one retry is worth it, and a second failure is
# a real failure rather than bad luck.
_EXTRACT_ATTEMPTS = 2

# A keyword longer than this is the model restating the question instead of
# naming its shape, which defeats the whole mechanic — every question would get
# its own key. Treated as a failed extraction rather than truncated, because
# truncating "case count by court" and "case count by judge" to three words
# merges two different intents into one key, which is the failure this module
# exists to prevent.
_KEYWORD_MAX_WORDS = 6
_KEYWORD_MIN_CHARS = 3

# Everything outside this class is deleted from the keyword. The class is the
# rail, not a formatting preference: no digit and no Arabic letter can survive
# it, so no year, case number or party name can reach a cache key regardless of
# what the model returns. See rail 1 in the module docstring.
_KEYWORD_ALLOWED = re.compile(r"[^a-z ]+")

# Punctuation stripped from the EDGES of a specific's value. Measured against
# real Gemma output: asked for the value verbatim, it returns "محكمة البداية؟"
# and "المستدعى ضده؟" — the question mark belongs to the sentence, not to the
# value. It matters beyond tidiness, because the plan is for sql_guard to check
# that each specific reached the generated SQL, and no SQL will ever contain a
# ؟. Stripped only at the edges: MS-2026-0301 must keep its hyphens.
_VALUE_EDGE_PUNCTUATION = " \t\n\r؟،؛.,!?:;\"'()[]{}«»…-_"

# Free-text keywords drift between synonyms, and every drift is a key that does
# not collide. Measured over 10 Arabic pairs on Gemma: 4 failures, ALL of this
# kind — "case list by year" vs "case listing by year", "case lookup by role"
# vs "case list by role", "case list for judge" vs "case listing by judge".
# None was a wrong merge; every one was a missed one.
#
# Folding the two open word classes is what turns those into collisions without
# asking the model to be more consistent than it is. It is deliberately small
# and derived from observed output rather than invented: add to it when a probe
# shows a new drift, not in anticipation of one.
_KEYWORD_STOPWORDS = frozenset(
    "by for of in on at the a an per with and to from within".split()
)

# Verbs and nouns Gemma uses interchangeably for one intent. Mapping them onto a
# single stem MERGES intents that are arguably distinct — "party lookup" (one
# row) and "party list" (many) land together — and that is acceptable here for a
# reason specific to APC: the cached plan is a starting point, and the adapter
# rewrites the SQL from the ORIGINAL question, which still says which was asked.
# A merge costs the adapter some work; a missed collision costs the whole
# feature.
_KEYWORD_SYNONYMS = {
    "listing": "list",
    "lookup": "list",
    "look": "list",
    "retrieve": "list",
    "fetch": "list",
    "get": "list",
    "show": "list",
    "find": "list",
    "search": "list",
    "total": "count",
    "tally": "count",
    "session": "hearing",
    "sitting": "hearing",
    "ranking": "rank",
    "top": "rank",
    "busiest": "rank",
    "highest": "rank",
    "details": "detail",
    "info": "detail",
    "information": "detail",
}

# Tokens that MUST be recoverable, matched deterministically so the guarantee
# does not depend on the model having tagged them.
#
#   - a four-digit year (the 2023/2024 case)
#   - a reference code: two or more Latin letters, then digit groups joined by
#     separators (MS-2026-0301, BAR/2019/44) — case_number and bar_number are
#     stored in Latin script (see the SQL prompt in services/llm.py)
#   - any run of two or more digits not already caught above
_LITERAL_PATTERNS = (
    re.compile(r"\b(?:1[89]|20)\d{2}\b"),
    re.compile(r"\b[A-Za-z]{2,}[-/_]\d+(?:[-/_]\d+)*\b"),
    re.compile(r"\d{2,}"),
)

# Party-role surface forms whose near-twins name the OPPOSITE party. Mirrors the
# `mustadei` and `mustadaa_diddahu` concepts in data/concepts.json — see
# test_intent.py::test_role_surfaces_still_cover_the_glossarys_party_concepts,
# which fails if the glossary grows a party role this list has not been told
# about.
#
# The bare "المدعى" is here and is NOT in the glossary: it is the form in the
# regression pair that query_cache.py documents, and a question can use it
# without the "عليه" that the glossary alias carries.
_ROLE_SURFACES = (
    "المستدعي",
    "المستدعى ضده",
    "المدعي",
    "المدعى",
    "المدعى عليه",
    "المشتكي",
    "المشتكى عليه",
    "الجهة المستدعية",
    "طالب الدعوى",
    "رافع الدعوى",
    "الخصم",
)

# Tags the model is asked to use. Not enforced — an unrecognized tag is kept
# rather than dropped, because the VALUE is what the adapter needs and losing it
# over a vocabulary mismatch would break the guarantee this module owes. The
# list exists to keep tagging stable enough that sql_guard can eventually assert
# on particular tags.
TAGS = (
    "year",
    "date",
    "case_number",
    "bar_number",
    "person_name",
    "role",
    "status",
    "court",
    "case_type",
    "area_of_law",
    "limit",
    "other",
)

# The tag applied by the deterministic backstop, so a value the model missed is
# visibly the rail's work and not the model's.
TAG_LITERAL = "literal"

# DELIBERATELY SHORT, and the shortness is a latency decision rather than a style
# one. This prompt is prefilled on EVERY question, before anything else can
# happen, so its token count sits on the critical path of the whole pipeline.
# Measured on this deployment: a ~700-token version of these same instructions
# cost ~10.9s per extraction against a ~1.1s floor for a trivial prompt — nearly
# all of it prefill. The cache it feeds only saves ~11.8s on a hit, so the long
# prompt put break-even at a hit rate no real deployment reaches.
#
# What was cut is the explanation; what was kept is every rule that changed the
# model's behaviour in the probe. The party-role paragraph in particular stays
# almost verbatim: it was 10/10 on role tagging, and an untagged role is the one
# failure this module cannot mechanically recover from.
_SYSTEM_PROMPT = """Extract the SHAPE of an Arabic legal-database question. Reply with ONLY \
this JSON object, no prose and no code fences:

{"intent_keyword": "case count by year", "specifics": [{"tag": "year", "value": "2024"}]}

intent_keyword: 2-4 ENGLISH lowercase words naming what is asked, with every concrete value \
removed, so two questions differing only in their values get the SAME keyword. Never put a \
number, year, date, name, code, court or party role in it. "كم قضية في 2023؟" and "كم قضية في \
2024؟" are both "case count by year".

specifics: every concrete value the keyword left out, copied VERBATIM from the question in its \
original script — not translated, not respelled. Tags: year, date, case_number, bar_number, \
person_name, role, status, court, case_type, area_of_law, limit, other. An empty list is normal.

PARTY ROLES ARE ALWAYS SPECIFICS. "المدعي" (plaintiff) and "المدعى" (the sued) are opposite \
parties differing by one letter; copy whichever the question used as a "role" specific. Same \
for المستدعي / المستدعى ضده and المشتكي / المشتكى عليه. Getting this wrong answers about the \
wrong side of a case."""


@dataclass(frozen=True)
class Specific:
    """One concrete value the keyword dropped, kept verbatim.

    `value` is the surface form as the user wrote it, deliberately un-normalized:
    the adapter has to put it back into SQL, and sql_guard has to be able to look
    for it there. A folded copy would match neither.
    """

    tag: str
    value: str


@dataclass
class Intent:
    """What a question is asking, split into the cacheable part and the rest.

    `keyword` is "" whenever this extraction must not be used as a cache key —
    the model failed, the keyword came back unusable, or a party role was named
    and not tagged. Callers treat an empty keyword as "no cache", never as a
    key of its own.
    """

    keyword: str
    specifics: list[Specific] = field(default_factory=list)
    # "model" — the model produced a usable keyword.
    # "unusable" — the model answered but the answer failed the rails.
    # "failed" — the model could not be reached or never returned JSON.
    source: str = "model"
    # Why an unusable extraction was rejected. Logged and surfaced in the trace
    # (stage 3); never shown to the user.
    reason: str = ""

    @property
    def cacheable(self) -> bool:
        return bool(self.keyword)

    def values_by_tag(self, tag: str) -> list[str]:
        return [s.value for s in self.specifics if s.tag == tag]

    def as_dict(self) -> dict:
        return {
            "keyword": self.keyword,
            "specifics": [{"tag": s.tag, "value": s.value} for s in self.specifics],
            "source": self.source,
            "reason": self.reason,
        }


def _unusable(reason: str, specifics: list[Specific], source: str = "unusable") -> Intent:
    """An extraction that must not key the cache, with its specifics kept.

    The specifics survive rejection on purpose: stage 3 logs them, and reading
    what WAS extracted is how you tell "the model tagged nothing" apart from
    "the model tagged everything and the keyword was still junk".
    """
    return Intent(keyword="", specifics=specifics, source=source, reason=reason)


# --- Canonicalisation ---------------------------------------------------------


def canonical_keyword(raw: str) -> str:
    """Fold a model-written keyword to its canonical form, or "" if unusable.

    RAIL 1. Lowercased, separators unified, then everything that is not an ASCII
    letter or a space is DELETED — which removes digits and Arabic script
    wholesale. That deletion is the guarantee: a year or an Arabic party name
    cannot be part of a cache key even when the model ignores every instruction
    and writes the question back verbatim.

    A keyword too long to be a shape is rejected rather than truncated. Cutting
    "case count by court" and "case count by judge" down to a shared prefix
    would merge two intents that need different plans, which is precisely the
    failure the whole module is built to avoid.
    """
    lowered = raw.lower().replace("_", " ").replace("-", " ")
    tokens = _KEYWORD_ALLOWED.sub(" ", lowered).split()

    # Counted BEFORE folding: an overlong keyword is evidence the model restated
    # the question instead of naming its shape, and folding would hide that by
    # dropping the connectives that made it long.
    if not tokens or len(tokens) > _KEYWORD_MAX_WORDS:
        return ""

    folded = [_fold_token(token) for token in tokens if token not in _KEYWORD_STOPWORDS]
    keyword = " ".join(token for token in folded if token)
    return keyword if len(keyword) >= _KEYWORD_MIN_CHARS else ""


def _fold_token(token: str) -> str:
    """One keyword word, singularized then mapped onto its synonym stem.

    Singularization runs first so "listings" reaches "listing" and then "list".
    The plural rules are the two that actually occur in English keywords and no
    more; a real stemmer would be a dependency and a much larger set of ways to
    merge two words that should have stayed apart.
    """
    if len(token) > 4 and token.endswith("ies"):
        token = token[:-3] + "y"
    elif len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        token = token[:-1]
    return _KEYWORD_SYNONYMS.get(token, token)


def _scrub_specifics(keyword: str, specifics: list[Specific]) -> str:
    """Remove any tagged value's own words from the keyword.

    Rail 1 already deletes digits and Arabic, so what this catches is a
    Latin-script specific the model promoted into the shape — the "ms" of
    "MS-2026-0301", or an English role written as "plaintiff lookup".

    Removing an English role word is not collateral damage, it is the mechanic
    working: "plaintiff lookup" and "defendant lookup" have to collapse to one
    key for the adapter to be what tells them apart. If scrubbing empties the
    keyword completely, the extraction is unusable and the question is answered
    from scratch.
    """
    banned = {
        token
        for specific in specifics
        for token in _KEYWORD_ALLOWED.sub(" ", specific.value.lower()).split()
    }
    if not banned:
        return keyword
    return " ".join(token for token in keyword.split() if token not in banned)


def keyword_hash(keyword: str) -> str:
    """The cache key for a canonical keyword, or "" if there is nothing to key.

    sha256 of the canonical form, matching query_cache.cache_key's shape so the
    two can share a keyspace and a panel.
    """
    if not keyword:
        return ""
    return hashlib.sha256(keyword.encode("utf-8")).hexdigest()


# --- Rails --------------------------------------------------------------------


def _literal_backstop(question: str, specifics: list[Specific]) -> list[Specific]:
    """Append any year, code or number the model failed to tag.

    RAIL 2, and the reason the 2023/2024 case is safe without trusting the
    model: rail 1 guarantees the digits are not in the key, and this guarantees
    they are still in the request. Between them, a numeric specific is
    structurally impossible to lose.

    Matching is on the raw question rather than a normalized copy, because these
    tokens are Latin-script and digit-bearing — normalization has nothing to do
    for them and Arabic-Indic digits are already folded by nothing here on
    purpose (the SQL prompt requires Western digits).
    """
    known = " ".join(specific.value for specific in specifics)
    added: list[Specific] = []

    for pattern in _LITERAL_PATTERNS:
        for match in pattern.findall(question):
            token = match if isinstance(match, str) else match[0]
            if token in known or any(token == extra.value for extra in added):
                continue
            # A token already inside a longer one that was tagged (the "2026" of
            # "MS-2026-0301") is covered — restoring the whole code restores it.
            if any(token in existing.value for existing in [*specifics, *added]):
                continue
            added.append(Specific(TAG_LITERAL, token))

    return [*specifics, *added]


def role_terms_in(question: str) -> list[str]:
    """Party-role surface forms this question names, if any.

    Compared under arabic.normalize_key — the STRICT normalizer — and that
    choice is the whole point. normalize() folds ى→ي, which is exactly what
    merges المدعي with المدعى; using it here would report both roles for either
    question and the rail would pass on a pair it exists to catch.
    """
    folded_question = arabic.normalize_key(question)
    return [
        surface
        for surface in _ROLE_SURFACES
        if arabic.normalize_key(surface) in folded_question
    ]


def _role_terms_covered(roles: list[str], specifics: list[Specific]) -> list[str]:
    """Role surfaces named by the question that no specific carries.

    Containment is checked both ways under the strict normalizer: a specific of
    "المدعى عليه" covers the bare "المدعى" the question also matched, and a
    specific of "المدعي" covers a question that wrote "المدعين".
    """
    folded_values = [arabic.normalize_key(specific.value) for specific in specifics]
    missing = []
    for surface in roles:
        folded = arabic.normalize_key(surface)
        if not any(folded in value or value in folded for value in folded_values if value):
            missing.append(surface)
    return missing


# --- Parsing ------------------------------------------------------------------


def parse_specifics(payload: object) -> list[Specific]:
    """Coerce whatever the model returned into tagged values.

    Three shapes are accepted, because small models produce all three and the
    VALUE is the thing that must not be lost:

        [{"tag": "year", "value": "2024"}]     the documented shape
        {"year": "2024"}                       an object keyed by tag
        ["2024"]                               bare values, tagged "other"

    Values are stringified and stripped; empties are dropped. Nothing raises —
    an unparseable specifics list yields [], and the literal backstop then
    recovers whatever was numeric.
    """
    specifics: list[Specific] = []

    def add(tag: object, value: object) -> None:
        text = str(value).strip(_VALUE_EDGE_PUNCTUATION)
        if not text:
            return
        label = str(tag).strip() or "other"
        if not any(s.tag == label and s.value == text for s in specifics):
            specifics.append(Specific(label, text))

    if isinstance(payload, dict):
        for tag, value in payload.items():
            if isinstance(value, (list, tuple)):
                for item in value:
                    add(tag, item)
            else:
                add(tag, value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            if isinstance(item, dict):
                add(item.get("tag", "other"), item.get("value", ""))
            else:
                add("other", item)

    return specifics


def build_intent(question: str, payload: dict) -> Intent:
    """Turn one model response into an Intent, applying every rail.

    Pure and synchronous, so the policy can be tested without LM Studio — the
    same split services/arabic.py uses. `extract` is the thin async wrapper.
    """
    specifics = _literal_backstop(question, parse_specifics(payload.get("specifics")))

    missing_roles = _role_terms_covered(role_terms_in(question), specifics)
    if missing_roles:
        # The المدعي/المدعى class. No rail can restore an untagged Arabic role,
        # so this question does not get a key at all.
        return _unusable(f"untagged party role: {', '.join(missing_roles)}", specifics)

    keyword = canonical_keyword(str(payload.get("intent_keyword") or ""))
    if not keyword:
        return _unusable("keyword was empty, too long, or had no ASCII content", specifics)

    keyword = _scrub_specifics(keyword, specifics)
    if len(keyword) < _KEYWORD_MIN_CHARS:
        return _unusable("keyword was nothing but specifics", specifics)

    return Intent(keyword=keyword, specifics=specifics, source="model")


# --- The call ------------------------------------------------------------------


def build_user_prompt(question: str) -> str:
    return f"Question: {question}\n\nJSON:"


async def extract(question: str) -> Intent:
    """Extract the cache key and the dropped specifics. Never raises.

    Runs on the planner model (services/llm.py's default, Gemma) rather than the
    adapter: this decides what a question IS, and getting it wrong costs either
    a wrong shared plan or a silently disabled cache. It is one short completion
    over one short question, which is the cheapest LLM call in the pipeline.
    """
    if not question.strip():
        return _unusable("empty question", [], source="failed")

    user_prompt = build_user_prompt(question)

    for attempt in range(1, _EXTRACT_ATTEMPTS + 1):
        try:
            payload = await llm.chat_json(_SYSTEM_PROMPT, user_prompt)
            return build_intent(question, payload)
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "Intent extraction returned unusable JSON (attempt %d/%d): %s",
                attempt,
                _EXTRACT_ATTEMPTS,
                exc,
            )
        except Exception as exc:  # noqa: BLE001 — a cache miss, never a failed question
            logger.warning(
                "Intent extraction failed (attempt %d/%d): %s", attempt, _EXTRACT_ATTEMPTS, exc
            )

    # Degrades to the behaviour the system had before APC: full pipeline, no
    # cache. The specifics are still backstopped so the trace records what was
    # in the question even when the model said nothing usable.
    return _unusable(
        "model did not return a usable extraction",
        _literal_backstop(question, []),
        source="failed",
    )
