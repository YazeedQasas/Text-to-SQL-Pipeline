"""The question → answer pipeline, expressed as a stream of stage events.

The pipeline is a single async generator so that both endpoints run exactly
the same sequence of steps — the only difference is whether the intermediate
stage events are forwarded to the client (/api/query/stream) or discarded
(/api/query). The generator yields `StageEvent`s as it goes and, finally, one
`QueryResponse`.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.models import ContextUsage, HistoryTurn, QueryResponse, RetrievedTable
from app.services import arabic, context, llm
from app.services.db import execute_select
from app.services.embeddings import embed_texts
from app.services.retrieval import (
    format_concepts_for_prompt,
    format_tables_for_prompt,
    search_concepts,
    search_with_carryover,
)
from app.services.sql_guard import SQLGuardError, enforce_select_only

logger = logging.getLogger(__name__)

# The ordered stage list is sent to the client before the run starts, so the UI
# can render the whole progress list (including not-yet-reached steps) without
# duplicating this definition in the frontend.
STAGES: list[dict[str, str]] = [
    {"id": "embedding", "label": "Embedding your question"},
    {"id": "concepts", "label": "Matching domain terms"},
    {"id": "retrieval", "label": "Searching the schema index"},
    {"id": "prompt", "label": "Building the schema prompt"},
    {"id": "sql_generation", "label": "Generating SQL"},
    {"id": "sql_validation", "label": "Checking the SQL is read-only"},
    {"id": "sql_execution", "label": "Running the query"},
    {"id": "answer_generation", "label": "Writing the answer"},
]


class PipelineError(Exception):
    """A user-facing pipeline failure, carrying the HTTP status to report."""

    def __init__(self, detail: str, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass
class StageEvent:
    stage: str
    status: str  # "started" | "completed"
    detail: str | None = None
    # Optional full text behind `detail` (e.g. the schema prompt summarised as
    # "4940 characters of schema context"), so the client can offer to expand it.
    content: str | None = None


@dataclass
class TokenEvent:
    """A partial chunk of LLM output, emitted while the model is still writing."""

    stage: str
    text: str


@dataclass
class UsageEvent:
    """How full the context window is, emitted as soon as the prompt is costed.

    Sent before the LLM calls rather than only with the result, so the meter has
    a value even when a turn fails — including the turn that fails *because* the
    window is full, which is exactly when the user needs to see it.
    """

    used_tokens: int
    limit_tokens: int
    history_turns: int


PipelineEvent = StageEvent | TokenEvent | UsageEvent | QueryResponse


CONTEXT_FULL_MESSAGE = (
    "امتلأت ذاكرة المحادثة. ابدأ محادثة جديدة للمتابعة."
    " (Conversation context is full — start a new chat.)"
)


async def run_pipeline(
    question: str, history: list[HistoryTurn] | None = None
) -> AsyncIterator[PipelineEvent]:
    # The browser replays the transcript; the backend keeps no session state.
    history = context.trim_history(list(history or []))

    yield StageEvent("embedding", "started")
    # The question and its fragments go in ONE request. LM Studio's cost here is
    # almost entirely per-call, not per-item — measured, a dozen fragments add
    # ~260ms to a ~2.7s call, while issuing them as a second call would add the
    # full 2.7s again.
    grams = arabic.content_grams(question)
    vectors = await embed_texts([question, *grams])
    query_vector = vectors[0]
    gram_vectors = list(zip(grams, vectors[1:]))
    yield StageEvent(
        "embedding",
        "completed",
        f"{len(query_vector)}-dimension vector, {len(grams)} question fragments",
    )

    # Runs before retrieval rather than alongside it: a matched concept steers
    # which tables are chosen, so the schema search needs the result. The cost
    # is one Qdrant search over a glossary of a few dozen points, which is
    # nothing next to the two LLM calls further down.
    yield StageEvent("concepts", "started")
    concepts = await search_concepts(question, gram_vectors)
    glossary = format_concepts_for_prompt(concepts)
    yield StageEvent(
        "concepts",
        "completed",
        # Most questions contain no domain term at all, and the empty case is
        # the expected one — say so plainly rather than showing a bare "0".
        # A lexical hit has no similarity score worth showing, so it is labelled
        # by how it was found instead of by a number that means nothing.
        ", ".join(
            f"{c.term} ({'exact' if c.matched_by == 'lexical' else f'{c.score:.2f}'})"
            for c in concepts
        )
        if concepts
        else "no domain terms matched",
        content=glossary or None,
    )

    yield StageEvent("retrieval", "started")
    # The last turn's tables are the ones the conversation is about; a follow-up
    # that names none of them would otherwise retrieve past its own subject.
    carry = list(history[-1].table_names) if history else []
    tables = await search_with_carryover(query_vector, carry, concepts)
    if not tables:
        raise PipelineError("No relevant tables found for this question.")
    yield StageEvent(
        "retrieval",
        "completed",
        f"{len(tables)} tables: " + ", ".join(t.table_name for t in tables),
    )

    yield StageEvent("prompt", "started")
    schema_context = format_tables_for_prompt(tables)

    # Cost the real prompt rather than an approximation of it, and refuse before
    # LM Studio has to truncate. Overflow there drops the head of the prompt —
    # the schema — so the model would answer with invented columns instead of
    # reporting that it lost the thread.
    sql_messages = llm.build_sql_messages(question, schema_context, history, glossary)
    budget = context.measure(sql_messages, len(history))
    yield UsageEvent(
        used_tokens=budget.used_tokens,
        limit_tokens=budget.limit_tokens,
        history_turns=budget.history_turns,
    )
    if budget.exhausted:
        logger.info(
            "Refused: context full at %d/%d tokens over %d turns",
            budget.used_tokens,
            budget.limit_tokens,
            budget.history_turns,
        )
        raise PipelineError(CONTEXT_FULL_MESSAGE, status_code=413)

    yield StageEvent(
        "prompt",
        "completed",
        f"{len(schema_context)} characters of schema context · "
        f"{budget.used_tokens}/{budget.limit_tokens} tokens, {len(history)} past turns",
        content=schema_context,
    )

    yield StageEvent("sql_generation", "started")
    sql_chunks: list[str] = []
    async for chunk in llm.stream_sql(question, schema_context, history, glossary):
        sql_chunks.append(chunk)
        yield TokenEvent("sql_generation", chunk)
    raw_sql = llm.strip_code_fences("".join(sql_chunks))
    refusal = llm.parse_no_query(raw_sql)
    if refusal is not None:
        # The model had the full schema in front of it and still could not
        # answer, so its reason is more specific than anything we could write
        # here — pass it to the user verbatim.
        logger.info("SQL generation declined the question: %s | question=%r", refusal, question)
        raise PipelineError(refusal)
    yield StageEvent("sql_generation", "completed")

    yield StageEvent("sql_validation", "started")
    try:
        safe_sql = enforce_select_only(raw_sql)
    except SQLGuardError as exc:
        logger.warning("Rejected generated SQL: %s | sql=%r", exc, raw_sql)
        raise PipelineError(f"Generated SQL was rejected: {exc}") from exc
    yield StageEvent("sql_validation", "completed", "single SELECT, row limit applied")

    yield StageEvent("sql_execution", "started")
    try:
        columns, rows = await execute_select(safe_sql)
    except Exception as exc:
        logger.warning("SQL execution failed: %s | sql=%r", exc, safe_sql)
        raise PipelineError(f"SQL execution failed: {exc}") from exc
    yield StageEvent("sql_execution", "completed", f"{len(rows)} rows returned")

    yield StageEvent("answer_generation", "started")
    answer_chunks: list[str] = []
    async for chunk in llm.stream_answer(question, safe_sql, columns, rows, history):
        answer_chunks.append(chunk)
        yield TokenEvent("answer_generation", chunk)
    answer = "".join(answer_chunks).strip()
    yield StageEvent("answer_generation", "completed")

    # Report what the *next* turn will start from: this exchange is about to
    # join the history the browser replays.
    next_budget = context.measure(
        llm.build_sql_messages(
            question,
            schema_context,
            [*history, HistoryTurn(question=question, sql=safe_sql, answer=answer)],
            glossary,
        ),
        len(history) + 1,
    )

    yield QueryResponse(
        answer=answer,
        sql=safe_sql,
        columns=columns,
        rows=rows,
        retrieved_tables=[
            RetrievedTable(table_name=t.table_name, description=t.description, score=t.score)
            for t in tables
        ],
        usage=ContextUsage(
            used_tokens=next_budget.used_tokens,
            limit_tokens=next_budget.limit_tokens,
            history_turns=next_budget.history_turns,
        ),
    )
