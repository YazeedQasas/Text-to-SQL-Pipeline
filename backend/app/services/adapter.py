"""The APC adapter: rebuild task-specific SQL from a cached plan template.

APC splits caching into two moments. The key is deliberately coarse, so
questions of one shape collide and share a stored plan; this is the other half,
which makes the result precise again. Everything the key threw away — the year,
the party, the case number, the comparison direction — is handed to this module
and has to come back out in the SQL.

That makes the adapter LOAD-BEARING FOR CORRECTNESS, not for cost. A cache that
merges "before 2020" with "after 2020" is only survivable because something
downstream restores the direction, and this is that something.

THE TEMPLATE IS EVIDENCE, NOT AN ANSWER
---------------------------------------
The single most important property here, and the one the tests hammer: the
adapter must generate from the ORIGINAL QUESTION and its specifics, never from
the template alone. The template is a worked example of a question that was
already answered — a different question, with different values, possibly asked
in the other direction. It shows which tables and which join path a question of
this shape needed. It does not say what to filter on.

An adapter that copies the template and swaps a literal is worse than no cache,
because it is confidently wrong exactly where the key was coarsest. So the
prompt names the template as belonging to a DIFFERENT question and states the
tie-break explicitly: where the example and the new question disagree, the
question wins.

WHY A SEPARATE, SMALLER MODEL
-----------------------------
Adapting a worked example to new values is a narrower job than planning a query
against a schema, and the fast path must not pay planner prices. Two measured
facts drove the choice (see config.ADAPTER_MODEL):

  - Gemma 4 E4B is a reasoning model and spent 389 of 432 completion tokens
    thinking about a trivial COUNT. That is the real cost behind stage 1's
    latency, and it is not something a cache can afford on its fast path.
  - Llama-3.1-8B-Instruct has no reasoning phase, and answered the same prompt
    immediately with clean SQL.

The adapter is also loaded at HALF the planner's context (8192 vs 16000), which
is why this module costs its own prompt rather than trusting services/context.py
— that module measures against the planner's window and would happily approve a
prompt this model will truncate.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not decide whether to use the cache, it does not write to it, and it
does not execute anything. `sql_guard.enforce_select_only` stays downstream and
is still the only thing standing between generated text and the database; a
refusal or a budget overrun here returns None so the caller runs the full
pipeline, which is always correct and merely slower.
"""

import logging
from dataclasses import dataclass, field

from app.config import (
    ADAPTER_CONTEXT_WINDOW_TOKENS,
    ADAPTER_MODEL,
    ADAPTER_OUTPUT_RESERVE_TOKENS,
)
from app.services import context, llm
from app.services.intent import Specific
from app.services.operators import Operator

logger = logging.getLogger(__name__)


# The instruction is English for the same reason _SQL_SYSTEM_PROMPT is: these
# are rules ABOUT SQL, and instruction-following on code generation is more
# reliable in English even when the data and the question are Arabic.
_SYSTEM_PROMPT = """You are a MySQL expert. You are given a worked example — a question that \
was answered before, and the SQL that answered it — plus a NEW question. Write the SQL for the \
NEW question.

THE EXAMPLE ANSWERS A DIFFERENT QUESTION. It is there to show you which tables and which join \
path a question of this shape needs. It is NOT the answer, and its filter values, its \
comparison direction and its aggregation may all be wrong for the new question.

Rules, in order of authority:
1. The NEW QUESTION is authoritative. Wherever it disagrees with the example, follow the \
question and change the SQL.
2. Every value under "Values in the new question" MUST appear in your SQL, used as the new \
question uses it. These are the parts the example is most likely to have wrong.
3. Every entry under "Required behaviour" MUST be honoured. If it says the comparison is \
LATER-THAN and the example filters earlier-than, you must flip it. If it says COUNT and the \
example selects rows, you must count. These are read directly from the new question.
4. Use only the tables and columns in the schema below. Never invent a name. If the example \
references something the schema no longer has, drop it.
5. Output ONLY the SQL query. No explanation, no markdown fences, no comments.
6. Only ever write a SELECT. Never INSERT, UPDATE, DELETE, DROP or ALTER.
7. If the new question cannot be answered from the schema, output NO_QUERY followed by a colon \
and one short Arabic sentence saying what is missing.

The user asks in Arabic and the schema descriptions are Arabic, but every table and column \
identifier is English. Use the English identifiers exactly as the schema writes them.

Category and ENUM columns accept only a fixed set of values, listed verbatim in each column's \
description. Copy such a value EXACTLY — a near-miss silently returns zero rows.

Match names and titles with LIKE and wildcards, never with equality. Where the schema lists a \
column whose name ends in _norm, match against that instead of the raw column, and normalize \
the search term the same way: bare alef ا, yaa ي, haa ه, no diacritics. Keep the raw column in \
the SELECT list for display."""


# How each canonical operator is described to the adapter. Written as an
# instruction about the QUERY rather than as the Arabic word, because the model
# is being asked to change SQL, not to parse Arabic — the parsing already
# happened in services/operators.py and this is its output.
_OPERATOR_INSTRUCTIONS = {
    "lt": "The comparison is EARLIER-THAN / LESS-THAN. Use < or <=, never > or >=.",
    "gt": "The comparison is LATER-THAN / GREATER-THAN. Use > or >=, never < or <=.",
    "between": "The filter is a RANGE between two bounds. Use BETWEEN, or two comparisons.",
    "count": "The question asks HOW MANY. Return a COUNT, not a list of rows.",
    "desc": "The question asks for the MOST / LARGEST / LONGEST. Order descending.",
    "asc": "The question asks for the LEAST / SMALLEST / SHORTEST. Order ascending.",
    "not": "The question is NEGATED. Exclude what it names rather than selecting it.",
    "exists": "The question asks WHETHER ANY exist. Return a count or an existence check.",
}


@dataclass
class PlanTemplate:
    """A previously-answered question of this shape, and the SQL that answered it.

    Stored in the cache under the coarse key. Kept as a QUESTION AND ITS SQL
    rather than as a parameterised skeleton with holes, for one reason: a
    skeleton with holes invites the adapter to fill the holes and change nothing
    else, which is the anchoring failure this whole module is written against.
    A worked example makes it obvious that the example belongs to a different
    question and can be departed from.

    Only `table_names` is carried alongside, never the table documents — those
    are re-fetched live, so a table re-documented since this entry was written
    contributes its current description. Same rule as query_cache.py.
    """

    keyword: str
    source_question: str
    source_sql: str
    table_names: list[str] = field(default_factory=list)


def _render_specifics(specifics: list[Specific]) -> str:
    if not specifics:
        return "  (none — the question names no specific values)"
    return "\n".join(f"  - {s.tag}: {s.value}" for s in specifics)


def _render_operators(operators: list[Operator]) -> str:
    """Required behaviour, deduplicated by canonical form.

    Deduplicated because two comparisons in one direction are one instruction,
    and repeating it adds tokens to a prompt that is already tight against an
    8192 window.
    """
    seen: list[str] = []
    for operator in operators:
        if operator.canonical not in seen:
            seen.append(operator.canonical)

    lines = [
        f"  - {_OPERATOR_INSTRUCTIONS[canonical]}"
        for canonical in seen
        if canonical in _OPERATOR_INSTRUCTIONS
    ]
    return "\n".join(lines) or "  (nothing beyond what the question says)"


def build_messages(
    template: PlanTemplate,
    question: str,
    specifics: list[Specific],
    schema_context: str,
    glossary: str = "",
    operators: list[Operator] = (),
) -> list[dict]:
    """The adapter prompt.

    Ordering is deliberate and mirrors services/llm.build_sql_messages: the
    schema first, then the example, then the glossary, and the NEW QUESTION
    LAST. LM Studio truncates an overlong prompt from the START, so whatever
    sits nearest the end survives longest — and the new question plus its
    required behaviour is precisely what must never be the thing that gets cut.
    """
    glossary_section = f"{glossary}\n\n" if glossary else ""

    user = (
        f"Schema:\n{schema_context}\n\n"
        f"{glossary_section}"
        "--- Worked example (a DIFFERENT question, answered earlier) ---\n"
        f"Question was: {template.source_question}\n"
        f"SQL was:\n{template.source_sql}\n\n"
        "--- The question you must answer now ---\n"
        f"New question: {question}\n\n"
        "Values in the new question (each MUST appear in your SQL):\n"
        f"{_render_specifics(specifics)}\n\n"
        "Required behaviour (read from the new question, overrides the example):\n"
        f"{_render_operators(list(operators))}\n\n"
        "SQL for the new question:"
    )

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def fits_budget(messages: list[dict]) -> bool:
    """Whether this prompt fits the ADAPTER's window, which is not the planner's.

    Checked here rather than through services/context.py because that module
    measures against CONTEXT_WINDOW_TOKENS — the planner's 16000 — and the
    adapter is loaded at 8192. Trusting it would approve a prompt this model
    truncates from the head, dropping the schema and leaving the adapter to
    invent column names.
    """
    limit = max(0, ADAPTER_CONTEXT_WINDOW_TOKENS - ADAPTER_OUTPUT_RESERVE_TOKENS)
    return context.estimate_messages(messages) <= limit


async def adapt(
    template: PlanTemplate,
    question: str,
    specifics: list[Specific],
    schema_context: str,
    glossary: str = "",
    operators: list[Operator] = (),
) -> str | None:
    """Rebuild SQL for `question` from a cached plan. None means "use the full pipeline".

    Never raises and never returns something unchecked. Every failure — a prompt
    too large for the adapter's window, an unreachable model, a refusal, an empty
    response — returns None, and the caller falls back to planning from scratch.
    That fallback is always correct, so there is no failure here worth surfacing
    to a user.

    The returned string is RAW MODEL OUTPUT with fences stripped. It has not been
    through sql_guard, and callers must still pass it there: this module is a
    generator like services/llm.py, not a gate.
    """
    messages = build_messages(
        template, question, specifics, schema_context, glossary, operators
    )

    if not fits_budget(messages):
        logger.info(
            "Adapter prompt does not fit the %d-token adapter window; using the full pipeline",
            ADAPTER_CONTEXT_WINDOW_TOKENS,
        )
        return None

    try:
        raw = await llm.collect(llm._chat_stream(messages, model=ADAPTER_MODEL))
    except Exception as exc:  # noqa: BLE001 — a cache miss, never a failed question
        logger.warning("Adapter call failed, falling back to the full pipeline: %s", exc)
        return None

    sql = llm.strip_code_fences(raw).strip()
    if not sql:
        logger.warning("Adapter returned nothing; falling back to the full pipeline")
        return None

    if llm.parse_no_query(sql) is not None:
        # The adapter declining is not the same as the planner declining. It was
        # shown one worked example, not the whole schema reasoning path, so its
        # refusal is weak evidence — the full pipeline gets to decide, and it is
        # the one whose refusal reaches the user.
        logger.info("Adapter declined the question; deferring to the full pipeline")
        return None

    return sql
