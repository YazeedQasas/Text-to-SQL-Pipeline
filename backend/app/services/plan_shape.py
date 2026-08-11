"""The literal-insensitive skeleton of a SQL query, and whether two match.

Shadow verification needs one primitive: given the SQL a cached plan produced
and the SQL the full pipeline would have produced, are they the SAME QUERY WITH
DIFFERENT VALUES, or two different queries?

Comparing the strings cannot answer that — every hit differs by at least the
literal it restored, which is the feature working. Comparing results cannot
answer it either: two wrong queries can return the same row count, and running
both to compare rows costs exactly what the cache was built to save.

So this reduces a query to the decisions a PLAN makes — which tables, which
aggregate, which comparison directions, whether it groups, orders, negates or
limits — and drops everything a plan leaves to the values. Two queries with the
same skeleton are the same plan; anything else is a different plan wearing the
same cache key.

WHY sqlparse RATHER THAN REGEX ALONE
------------------------------------
String literals. A query filtering on `title LIKE '%قبل%'` contains the word the
operator tagger cares about, and `status = '>'` contains a comparison operator,
inside quotes where neither means anything. sqlparse is already a dependency
(it backs services/sql_guard.py), and it can tell a literal from an operator,
which no amount of regex over raw SQL can do safely.

Literals are replaced with a placeholder BEFORE any feature is read, so the rest
of this module can use plain string matching without that class of false
positive.

WHAT THIS IS NOT
----------------
Not a correctness check. Two queries with identical skeletons can still answer
different questions — same tables, same aggregate, different WHERE column. This
narrows "did the plan hold?" to something mechanically checkable and cheap; it
does not certify an answer. The full pipeline's SQL is the reference, and this
says whether the adapted query agreed with it about the plan.
"""

import re
from dataclasses import dataclass, field

import sqlparse
from sqlparse import tokens as T

# What a literal becomes before features are read. Deliberately not a value that
# could itself look like a feature.
_PLACEHOLDER = "?"

# Aggregate functions worth telling apart. COUNT vs a bare projection is the
# list-versus-count distinction the operator tagger also tracks, and the two
# agreeing is a signal in itself.
_AGGREGATES = ("COUNT", "SUM", "AVG", "MIN", "MAX", "GROUP_CONCAT")

# Comparison operators, longest first so ">=" is never read as ">".
_COMPARATORS = (">=", "<=", "<>", "!=", "=", ">", "<")

_TABLE_AFTER = re.compile(r"\b(?:FROM|JOIN)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?", re.IGNORECASE)


@dataclass(frozen=True)
class Skeleton:
    """A query's plan-level decisions, with every value erased.

    Frozen and comparable: `a == b` is the whole question shadow verification
    asks. The fields are sets and booleans rather than an AST because the
    comparison has to be order-insensitive — `WHERE a > 1 AND b < 2` and
    `WHERE b < 2 AND a > 1` are one plan.
    """

    tables: frozenset[str] = frozenset()
    aggregates: frozenset[str] = frozenset()
    comparators: frozenset[str] = frozenset()
    order_directions: frozenset[str] = frozenset()
    has_group_by: bool = False
    has_order_by: bool = False
    has_limit: bool = False
    has_between: bool = False
    has_like: bool = False
    has_negation: bool = False
    has_join: bool = False
    has_subquery: bool = False

    def differences(self, other: "Skeleton") -> list[str]:
        """Field names on which two skeletons disagree, for the trace.

        A verdict of "diverged" is useless without saying where; this is what
        turns a shadow failure into something a person can act on.
        """
        diffs = []
        for name in self.__dataclass_fields__:
            mine, theirs = getattr(self, name), getattr(other, name)
            if mine != theirs:
                diffs.append(f"{name}: {_render(mine)} != {_render(theirs)}")
        return diffs


def _render(value: object) -> str:
    if isinstance(value, frozenset):
        return "{" + ", ".join(sorted(str(item) for item in value)) + "}" if value else "{}"
    return str(value)


def _strip_literals(sql: str) -> str:
    """Uppercased SQL with every string and number replaced by a placeholder.

    This is the step that makes everything downstream safe to match with plain
    substring tests: after it, a `<` is an operator and never the contents of a
    quoted Arabic string.
    """
    parsed = sqlparse.parse(sqlparse.format(sql, strip_comments=True))
    if not parsed:
        return ""

    pieces: list[str] = []
    for token in parsed[0].flatten():
        if token.ttype in T.Literal.String or token.ttype in T.Literal.Number:
            pieces.append(_PLACEHOLDER)
        elif token.ttype in T.Comment:
            pieces.append(" ")
        else:
            pieces.append(token.value)

    return re.sub(r"\s+", " ", "".join(pieces)).strip().upper()


def skeleton(sql: str) -> Skeleton:
    """Reduce a query to its plan. Never raises; unparseable SQL yields an empty
    skeleton, which compares unequal to everything real and so fails toward
    "these do not match"."""
    try:
        normalized = _strip_literals(sql)
    except Exception:  # noqa: BLE001 — a shadow check must never break a request
        return Skeleton()

    if not normalized:
        return Skeleton()

    tables = frozenset(name.lower() for name in _TABLE_AFTER.findall(normalized))
    aggregates = frozenset(name for name in _AGGREGATES if f"{name}(" in normalized)
    comparators = frozenset(op for op in _COMPARATORS if _contains_operator(normalized, op))

    directions = set()
    if "ORDER BY" in normalized:
        tail = normalized.split("ORDER BY", 1)[1]
        # MySQL's default is ascending, so an ORDER BY with no keyword is ASC —
        # recording it explicitly keeps "ORDER BY x" and "ORDER BY x ASC" equal.
        directions.add("DESC" if "DESC" in tail else "ASC")

    return Skeleton(
        tables=tables,
        aggregates=aggregates,
        comparators=comparators,
        order_directions=frozenset(directions),
        has_group_by="GROUP BY" in normalized,
        has_order_by="ORDER BY" in normalized,
        has_limit="LIMIT" in normalized,
        has_between="BETWEEN" in normalized,
        has_like="LIKE" in normalized,
        has_negation=any(marker in normalized for marker in (" NOT ", "!=", "<>")),
        has_join="JOIN" in normalized,
        has_subquery=normalized.count("SELECT") > 1,
    )


def _contains_operator(normalized: str, operator: str) -> bool:
    """Whether `operator` appears as itself, not as part of a longer one.

    Without this, every `>=` would also register as `>`, and two plans that
    genuinely differ on strict-versus-inclusive would compare equal.
    """
    for match in re.finditer(re.escape(operator), normalized):
        start, end = match.start(), match.end()
        before = normalized[start - 1] if start else ""
        after = normalized[end] if end < len(normalized) else ""
        if before in "<>!=" or after in "<>=":
            continue
        return True
    return False


@dataclass
class ShapeComparison:
    """Whether two queries are the same plan, and where they part company."""

    matches: bool
    differences: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.matches


def compare(adapted_sql: str, reference_sql: str) -> ShapeComparison:
    """Do these two queries make the same plan-level decisions?

    `reference_sql` is the full pipeline's output — the thing being trusted.
    `adapted_sql` is what the cached plan produced. The asymmetry is only in the
    naming and the message; the comparison itself is symmetric.
    """
    adapted, reference = skeleton(adapted_sql), skeleton(reference_sql)
    if adapted == reference:
        return ShapeComparison(True)
    return ShapeComparison(False, adapted.differences(reference))
