"""Operator words: the tokens that decide a SQL skeleton, not a SQL literal.

APC keys on the SHAPE of a question and drops the specifics, so questions of one
shape share a plan. The counterexample hunt found the class that breaks:

    ما هي القضايا قبل 2020؟   ->  key 'قضايا |literal'   specifics [2020]
    ما هي القضايا بعد 2020؟   ->  key 'قضايا |literal'   specifics [2020]

Identical key, identical specifics, opposite predicates. `قبل` and `بعد` sit in
arabic.STOPWORDS — a list tuned for glossary search, where dropping them is
right because they never help match a domain term. Reused to build a cache key
they erase the comparison direction, which is the ى→ي mistake one layer up.

VALUES COLLIDE, OPERATORS DO NOT
--------------------------------
This module exists to keep the two apart:

    a VALUE     (2024, MS-2026-0301, المدعي)  is REMOVED from the key and carried
                in `specifics` — the collision is the entire point of APC.
    an OPERATOR (قبل, كم, الأكثر, غير, هل)     is CANONICALISED INTO the key, so
                two questions that differ by one can never share a plan.

Note what canonicalising buys over merely keeping the word. `بعد 2020`, `منذ
2020` and `أكثر من 2020` are three spellings of one predicate; as raw words they
make three keys, as `gt` they make one. The operator is normalised for the same
reason a value is dropped — to collide with its paraphrases — while still being
unable to collide with its opposite.

A tagged operator also travels in the request, which is what gives sql_guard
something to check: `gt` in, `>` or `>=` expected out.

AN OPERATOR NEEDS AN OPERAND
----------------------------
The rule that makes lexical tagging work in Arabic, where these surface forms
are also ordinary words:

    من قبل المحكمة          قبل is "by" (agent marker)
    لم يُفصل فيها بعد        بعد is "yet"
    القضايا تحت المراقبة     تحت is part of a glossary alias, not a comparison
    حقوق الغير              الغير is "third party", not negation

A comparison compares against a value, so it only counts when a NUMBER follows
it. That operand is exactly the literal intent.py's backstop already extracts,
so the two mechanisms check each other rather than being two independent guesses.

Measured over 24 hand-built cases: bare lexical matching scored 67% precision
and 86% recall; requiring the operand scored 100/100. That says the rule is
sound, NOT that the problem is solved — the cases were written alongside the
rules, and this needs a real question corpus before anyone trusts the number.

WHY BEING WRONG HERE IS SURVIVABLE
----------------------------------
The two failure directions are not symmetric, and the asymmetry is the whole
safety argument:

    MISSED an operator   -> the word was never on the key whitelist, so it stays
                            in the key as an ordinary content word, the keys
                            differ, and the two questions simply do not share a
                            plan. Costs a cache hit.
    TAGGED a non-operator -> the word is REMOVED from the key, the key gets
                            coarser, and a merge that should not happen can.

So the key whitelist is the safety net for this module, not the other way round.
Recall failures here are free; precision failures are the ones to fear, which is
why every rule below is written to abstain rather than guess.
"""

import re
from dataclasses import dataclass

from app.services import arabic

# --- Canonical vocabulary -----------------------------------------------------
# What goes into the key. Deliberately tiny and SQL-shaped rather than
# Arabic-shaped: the point is that every spelling of "later than" arrives here
# as one token.

LT = "lt"  # <  /  <=
GT = "gt"  # >  /  >=
BETWEEN = "between"
COUNT = "count"  # COUNT(*) rather than a projection
DESC = "desc"  # ORDER BY ... DESC
ASC = "asc"
NOT = "not"
EXISTS = "exists"

KIND_COMPARISON = "comparison"
KIND_RANGE = "range"
KIND_AGGREGATION = "aggregation"
KIND_ORDERING = "ordering"
KIND_NEGATION = "negation"
KIND_EXISTENCE = "existence"

# SQL each canonical token asserts. Consumed by the verification step: a request
# carrying `gt` whose SQL contains no `>` or `>=` did not restore its operator,
# and the cached plan it came from must not be trusted for this question.
#
# Checked as a fail-SOFT consistency rule, never as a hard rejection like
# sql_guard's SELECT-only rule. A false positive in this module would otherwise
# turn a perfectly answerable question into a user-visible error.
SQL_EVIDENCE: dict[str, tuple[str, ...]] = {
    LT: ("<", "<="),
    GT: (">", ">="),
    BETWEEN: ("BETWEEN",),
    COUNT: ("COUNT(",),
    DESC: ("DESC",),
    ASC: ("ASC",),
    NOT: ("NOT", "!=", "<>"),
    EXISTS: ("EXISTS", "COUNT("),
}

# Comparison words, already normalised (arabic.normalize_key folds the hamza, so
# these are stored in the folded form the tokens will actually have).
_COMPARISON = {
    "قبل": LT,
    "حتى": LT,
    "تحت": LT,
    "دون": LT,
    "بعد": GT,
    "منذ": GT,
    "فوق": GT,
}

# Words that are a COMPARATIVE when followed by "من <number>" and a SUPERLATIVE
# otherwise. "أكثر من 100" is `gt`; "الأكثر قضايا" is `desc`.
_MAGNITUDE = {
    "اكثر": (GT, DESC),
    "اكبر": (GT, DESC),
    "اعلى": (GT, DESC),
    "اطول": (GT, DESC),
    "اقل": (LT, ASC),
    "اصغر": (LT, ASC),
    "ادنى": (LT, ASC),
    "اقصر": (LT, ASC),
}

_NEGATION = {"غير", "لا", "ليس", "بدون"}

# How far after an operator word its numeric operand may sit. Two tokens covers
# "بعد عام 2020" (after the year 2020) without reaching across a clause into an
# unrelated number — "قبل الجلسة في 2020" stays untagged, which is the abstain
# the module is supposed to make.
_OPERAND_WINDOW = 2

_NUMBER = re.compile(r"^\d+$")

# Conjunctions attach to the FRONT of the following word in Arabic, so "and
# before 2020" is written "وقبل 2020" as a single token and `قبل` never appears
# on its own. Measured: "القضايا بعد 2018 وقبل 2020" tagged only `gt`, losing
# the upper bound entirely.
#
# Only و and ف are stripped, and only when what remains is EXACTLY an operator
# word — the lexicon is a small closed set, so "وقبل" -> "قبل" is safe while
# nothing turns an ordinary noun into a false operator. The other proclitics
# (ب ل ك) are left alone: "بدون" would strip to "دون" and flip a negation into a
# comparison, which is the precision failure this module must not make.
_CONJUNCTION_PROCLITICS = ("و", "ف")

_OPERATOR_WORDS = (
    set(_COMPARISON) | set(_MAGNITUDE) | _NEGATION | {"بين", "عدد", "كم", "هل", "من"}
)


def _operator_form(token: str) -> str:
    """The token as the lexicon would recognise it, conjunction stripped.

    The whole token is tested FIRST, so a word that is itself an operator is
    never re-read as a prefixed one — "بدون" stays negation and does not become
    "دون".
    """
    if token in _OPERATOR_WORDS:
        return token
    if len(token) > 2 and token[0] in _CONJUNCTION_PROCLITICS:
        stripped = token[1:]
        if stripped in _OPERATOR_WORDS:
            return stripped
    return token


@dataclass(frozen=True)
class Operator:
    """One operator found in a question.

    `token` is the Arabic surface form, kept for the trace and for explaining a
    key to a human. `canonical` is what reaches the cache key — the whole point
    is that many tokens map onto one canonical.
    """

    kind: str
    token: str
    canonical: str


def _is_number(token: str) -> bool:
    return bool(_NUMBER.match(token))


def _operand_follows(tokens: list[str], index: int) -> bool:
    """Whether a number sits within the operand window after `index`."""
    window = tokens[index + 1 : index + 1 + _OPERAND_WINDOW + 1]
    return any(_is_number(token) for token in window)


def _comparative_operand(tokens: list[str], index: int) -> bool:
    """Whether this magnitude word is followed by "من <number>".

    That is what separates the comparative ("أكثر من 100 يوم", a predicate) from
    the superlative ("الأكثر قضايا", an ordering) — two different SQL shapes off
    one Arabic root.
    """
    if index + 1 >= len(tokens) or tokens[index + 1] != "من":
        return False
    return any(_is_number(token) for token in tokens[index + 2 : index + 2 + _OPERAND_WINDOW])


def extract_operators(question: str) -> list[Operator]:
    """Every operator in a question, in the order they appear. Never raises.

    COMPOUND BY DESIGN. "كم عدد القضايا بعد 2020؟" carries an aggregation AND a
    comparison, and returning only the first would drop the other from the key —
    reintroducing exactly the wrong-merge class this module exists to close.
    Callers get a list and are expected to fold all of it into the key.
    """
    raw = arabic.words(question)
    surface = arabic.words(arabic.normalize_key(question))
    # Every rule below reads `tokens`, the conjunction-stripped view, so that
    # "وقبل" is recognised as "قبل". `surface` keeps what the user actually
    # wrote, for the operand offsets and the trace.
    tokens = [_operator_form(token) for token in surface]
    found: list[Operator] = []

    def add(kind: str, index: int, canonical: str) -> None:
        found.append(Operator(kind, surface[index] if index >= 0 else "", canonical))

    # Existence: a sentence-initial particle only. "هل" mid-sentence is reported
    # speech ("سألت هل..."), not a question about existence.
    if tokens and tokens[0] == "هل":
        add(KIND_EXISTENCE, 0, EXISTS)

    # Aggregation: "كم" leading the question, or the noun "عدد" anywhere. Both
    # ask for a count rather than a projection, which is a different SELECT.
    if tokens and tokens[0] == "كم":
        add(KIND_AGGREGATION, 0, COUNT)
    elif "عدد" in tokens:
        add(KIND_AGGREGATION, tokens.index("عدد"), COUNT)

    # Range: "بين <n> و <n>". Needs BOTH operands — "الفرق بين المحكمتين" is
    # "between two courts" and bounds nothing.
    if "بين" in tokens:
        index = tokens.index("بين")
        numbers = [token for token in tokens[index + 1 :] if _is_number(token)]
        if len(numbers) >= 2:
            add(KIND_RANGE, index, BETWEEN)

    for index, token in enumerate(tokens):
        # "من قبل" is the agent marker "by", not a comparison. Checked before
        # anything else, because the operand rule alone would not save
        # "الحكم الصادر من قبل محكمة 2020" if a year happened to follow.
        if token == "قبل" and index > 0 and tokens[index - 1] == "من":
            continue

        if token in _COMPARISON and _operand_follows(tokens, index):
            add(KIND_COMPARISON, index, _COMPARISON[token])
            continue

        if token in _MAGNITUDE:
            comparative, superlative = _MAGNITUDE[token]
            if _comparative_operand(tokens, index):
                add(KIND_COMPARISON, index, comparative)
            else:
                add(KIND_ORDERING, index, superlative)
            continue

        if token in _NEGATION:
            # "الغير" is the legal term for a third party, a noun. The definite
            # article is stripped by normalize_key, so the raw token is what
            # tells them apart.
            if token == "غير" and any(word.startswith("الغ") for word in raw):
                continue
            # A negation particle with nothing after it is negating nothing.
            if index + 1 < len(tokens):
                add(KIND_NEGATION, index, NOT)

    return found


def key_tokens(operators: list[Operator]) -> list[str]:
    """The canonical tokens to fold into a cache key: sorted, deduplicated.

    Sorted so that word order in the question cannot change the key — "قبل 2020
    وبعد 2018" and "بعد 2018 وقبل 2020" bound the same range and must share a
    plan. Deduplicated because two comparisons in one direction are one fact
    about the shape.
    """
    return sorted({operator.canonical for operator in operators})


def missing_evidence(operators: list[Operator], sql: str) -> list[str]:
    """Canonical operators whose SQL evidence is absent from `sql`.

    The verification half. An empty list means every operator the question
    carried is visible in the generated SQL; a non-empty one means the plan did
    not restore something the question asked for.

    CALLERS MUST FAIL SOFT — drop the cached plan and run the full pipeline.
    This is a cache-consistency check, not a security boundary: sql_guard's
    SELECT-only rule may reject a query outright, and this one may not, because
    a false positive here is a wrongly-tagged ordinary word rather than a write
    statement.
    """
    upper = sql.upper()
    missing = []
    for canonical in key_tokens(operators):
        evidence = SQL_EVIDENCE.get(canonical, ())
        if evidence and not any(marker in upper for marker in evidence):
            missing.append(canonical)
    return missing
