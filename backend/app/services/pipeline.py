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

from app.config import QUERY_CACHE_ENABLED, QUERY_CACHE_FOLLOW_UPS
from app.models import ContextUsage, HistoryTurn, QueryResponse, RetrievedTable
from app.services import arabic, chat_store, context, llm, query_cache
from app.services.chat_store import ChatNotFound
from app.services.db import execute_select
from app.services.embeddings import embed_texts
from app.services.retrieval import (
    Concept,
    SchemaTable,
    concepts_by_terms,
    fetch_tables_by_name,
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
#
# Labels and details are Arabic because they are read by the end user on the Ask
# page, next to an Arabic question and an Arabic answer — this is the product's
# "thinking" text, not a developer log. Anything that would leak a table name, a
# row count or a SQL fragment into that text is deliberately left out: those are
# admin-side facts, and the reader of this page has no use for them.
STAGES: list[dict[str, str]] = [
    {"id": "embedding", "label": "تحليل السؤال"},
    {"id": "concepts", "label": "مطابقة المصطلحات القانونية"},
    {"id": "retrieval", "label": "البحث عن المعلومات ذات الصلة"},
    {"id": "prompt", "label": "تجهيز المعلومات"},
    {"id": "sql_generation", "label": "إعداد عملية البحث"},
    {"id": "sql_validation", "label": "التحقق من سلامة العملية"},
    {"id": "sql_execution", "label": "استخراج البيانات"},
    {"id": "answer_generation", "label": "كتابة الإجابة"},
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


CONTEXT_FULL_MESSAGE = "امتلأت ذاكرة المحادثة. ابدأ محادثة جديدة للمتابعة."

# Shown against every stage a cache hit skips. The stages are still reported as
# completed rather than omitted: the client renders the whole ordered list from
# STAGES before the run starts, so a silently missing step reads as a stall.
# Saying which ones were reused is also the only way the user can tell that the
# fast answer came from a previous question rather than from nowhere.
REUSED_DETAIL = "أُعيد استخدام سؤال سابق مطابق"

# Failures the user sees. The exception text behind each one is English, often a
# raw driver message, so it goes to the log and never to the page.
NO_TABLES_MESSAGE = "لم يتم العثور على معلومات ذات صلة بهذا السؤال."
SQL_REJECTED_MESSAGE = "تعذّر إتمام هذا الطلب لأنه لم يجتز فحص السلامة."
SQL_FAILED_MESSAGE = "تعذّر استخراج البيانات المطلوبة."


async def _reuse_cached_plan(
    question: str,
) -> tuple[str, list[SchemaTable], list[Concept]] | None:
    """The stored SQL for a question already asked, if it is still usable.

    Returns None whenever anything about the entry no longer holds, and the
    caller then runs the full pipeline. Falling back is always correct; reusing
    a plan that no longer fits the schema is not.

    The stored SQL is re-validated rather than trusted. It passed the guard when
    it was written, but the cache is a file on disk that a person can edit, and
    the guard is the only thing between generated text and the database.
    """
    entry = await query_cache.lookup(question)
    if entry is None:
        return None

    try:
        safe_sql = enforce_select_only(entry["sql"])
    except SQLGuardError as exc:
        logger.warning("Dropped a cached query that no longer passes the guard: %s", exc)
        await query_cache.remove(entry["key"])
        return None

    # Only the NAMES were cached, so this picks up the CURRENT description of
    # each table — a table re-documented since the entry was written contributes
    # its new text, not the text that was current when it was first asked about.
    table_names = list(entry.get("table_names", []))
    tables = await fetch_tables_by_name(table_names)
    if len(tables) != len(table_names):
        # A table the stored SQL depends on is no longer indexed. Invalidation
        # normally catches this first; this is the backstop for the case where
        # it did not.
        logger.info("Dropped a cached query whose tables are no longer indexed: %s", table_names)
        await query_cache.remove(entry["key"])
        return None

    return safe_sql, tables, await concepts_by_terms(list(entry.get("concept_terms", [])))


async def _answer_from_cache(
    question: str,
    history: list[HistoryTurn],
    safe_sql: str,
    tables: list[SchemaTable],
    concepts: list[Concept],
) -> AsyncIterator[PipelineEvent]:
    """Run the back half of the pipeline against a plan that already existed."""
    glossary = format_concepts_for_prompt(concepts)
    schema_context = format_tables_for_prompt(tables)

    # Reported as completed rather than skipped — see REUSED_DETAIL. The client
    # tolerates a "completed" with no matching "started"; the stage simply shows
    # as done with no duration, which is exactly what happened.
    for stage in ("embedding", "concepts", "retrieval", "prompt", "sql_generation"):
        yield StageEvent(stage, "completed", REUSED_DETAIL)
    yield StageEvent("sql_validation", "completed", "عملية قراءة فقط، دون أي تعديل على البيانات")

    budget = context.measure(
        llm.build_sql_messages(question, schema_context, history, glossary), len(history)
    )
    yield UsageEvent(
        used_tokens=budget.used_tokens,
        limit_tokens=budget.limit_tokens,
        history_turns=budget.history_turns,
    )

    async for event in _execute_and_answer(
        question, safe_sql, tables, schema_context, glossary, history, concepts, store=False
    ):
        yield event


async def _execute_and_answer(
    question: str,
    safe_sql: str,
    tables: list[SchemaTable],
    schema_context: str,
    glossary: str,
    history: list[HistoryTurn],
    concepts: list[Concept],
    store: bool,
) -> AsyncIterator[PipelineEvent]:
    """Everything after the SQL exists, shared by the generated and cached paths.

    The query is executed and the answer written from scratch every time,
    including on a cache hit. What is reused is the QUERY, never its results —
    a stored row count would go stale the moment the database moved, and a
    confidently outdated number out of a legal system is worse than a slow one.
    """
    yield StageEvent("sql_execution", "started")
    try:
        columns, rows = await execute_select(safe_sql)
    except Exception as exc:
        logger.warning("SQL execution failed: %s | sql=%r", exc, safe_sql)
        raise PipelineError(SQL_FAILED_MESSAGE) from exc
    yield StageEvent("sql_execution", "completed", "تم استخراج البيانات المطلوبة")

    yield StageEvent("answer_generation", "started")
    answer_chunks: list[str] = []
    async for chunk in llm.stream_answer(question, safe_sql, columns, rows, history):
        answer_chunks.append(chunk)
        yield TokenEvent("answer_generation", chunk)
    answer = "".join(answer_chunks).strip()
    yield StageEvent("answer_generation", "completed")

    # Only a run that got all the way here is worth keeping. A refusal, a
    # rejected query or a failed execution says nothing about whether the
    # retrieval behind it was right, so there is nothing to learn from it.
    if store:
        await query_cache.store(
            question,
            safe_sql,
            [table.table_name for table in tables],
            [concept.term for concept in concepts],
        )

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


async def run_chat_turn(chat_id: str, question: str) -> AsyncIterator[PipelineEvent]:
    """Run a question inside a stored conversation.

    This is the whole of what "each chat has its own context window" means in
    code: the history is read from THIS chat, so the budget measured in
    run_pipeline is measured over this chat's turns and no others. Nothing about
    the pipeline itself changed to make that true — it is a consequence of what
    it is handed, which is why run_pipeline below stays stateless and unaware
    that chats exist.

    The turn is appended BEFORE the result is yielded, so `saved` on the response
    is a fact rather than a promise. A write failure is logged and the answer is
    still delivered — the user asked a question and it was answered correctly,
    and throwing that away because a row could not be written would be a worse
    outcome than telling them it was not kept.
    """
    stored = await chat_store.load_history(chat_id)
    history = [
        HistoryTurn(
            question=turn["question"],
            sql=turn["sql"],
            answer=turn["answer"],
            table_names=turn["table_names"],
        )
        for turn in stored
    ]

    async for event in run_pipeline(question, history):
        if not isinstance(event, QueryResponse):
            yield event
            continue

        saved = False
        try:
            await chat_store.append_turn(
                chat_id,
                question,
                event.sql,
                event.answer,
                [table.table_name for table in event.retrieved_tables],
            )
            saved = True
        except ChatNotFound:
            # The chat was deleted while this answer was being written — routine
            # once chats stream concurrently. Not an error worth a stack trace.
            logger.info("Chat %s was deleted before its turn could be saved", chat_id)
        except Exception:  # noqa: BLE001 — a failed save must not lose a good answer
            logger.exception("Could not save a turn to chat %s", chat_id)

        yield event.model_copy(update={"saved": saved})


async def run_pipeline(
    question: str, history: list[HistoryTurn] | None = None
) -> AsyncIterator[PipelineEvent]:
    """Answer a question against a transcript, holding no state of its own.

    Callers supply the history: run_chat_turn reads it from a stored chat, while
    /api/query can be handed one in the request body. Which chat — or whether
    there is a chat at all — is deliberately invisible here.
    """
    supplied = list(history or [])

    # Cache policy is decided from what the CALLER supplied, before trimming —
    # never from `history` below.
    #
    # The coupling this avoids was real: context.trim_history returns [] for any
    # CONTEXT_MAX_TURNS <= 0, so a chat with a hundred stored turns arrived here
    # looking like a first turn. `not history` then read True, and a value that
    # presents itself as a context knob — and that reads like "unlimited" —
    # silently switched the cache's write guard to store-everything, putting
    # context-dependent queries where a later unrelated chat could pick them up.
    #
    # Deriving both cache decisions from the untrimmed input decouples them, so
    # CONTEXT_MAX_TURNS can only ever affect what the MODEL sees.
    has_prior_turns = bool(supplied)
    history = context.trim_history(supplied)

    # Reading and writing the cache are governed by DIFFERENT rules.
    #
    # WRITING is first-turn-only (see the `store=` argument further down). Note
    # what that does and does not guarantee: it guarantees the entry was written
    # by someone asking with no prior turns, which is NOT the same as the entry
    # being context-free. Nothing inspects the question, so opening a fresh chat
    # with "وكم منها مفتوحة؟" writes an anaphoric query to a cache every other
    # chat can read. This was verified reachable, and it is why the read rule
    # below no longer defaults to permissive.
    #
    # READING is now first-turn-only as well by default (QUERY_CACHE_FOLLOW_UPS
    # defaults false, see config.py). The lookup takes the question and nothing
    # else — no chat id, no history — so a chat deep in a conversation asking
    # words that match an entry gets that entry's SQL regardless of what its own
    # conversation was about. Blocking reads whenever there are prior turns is
    # blunt, and it costs most of the hit rate, but it is the only rule here that
    # does not depend on an assumption about how people phrase first questions.
    #
    # Set QUERY_CACHE_FOLLOW_UPS=true to restore reads on later turns. That is a
    # hit-rate-for-correctness trade, and the correctness being traded is
    # specifically: a question whose words match cold-start SQL built for a
    # different conversation.
    if QUERY_CACHE_ENABLED and (QUERY_CACHE_FOLLOW_UPS or not has_prior_turns):
        reused = await _reuse_cached_plan(question)
        if reused is not None:
            async for event in _answer_from_cache(question, history, *reused):
                yield event
            return

    yield StageEvent("embedding", "started")
    # The question and its fragments go in ONE request. LM Studio's cost here is
    # almost entirely per-call, not per-item — measured, a dozen fragments add
    # ~260ms to a ~2.7s call, while issuing them as a second call would add the
    # full 2.7s again.
    grams = arabic.content_grams(question)
    vectors = await embed_texts([question, *grams])
    query_vector = vectors[0]
    gram_vectors = list(zip(grams, vectors[1:]))
    yield StageEvent("embedding", "completed", "تمت قراءة السؤال وتحليل مقاطعه")

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
        # the expected one — say so plainly rather than showing a bare "0". The
        # matched terms themselves are worth naming: they are Arabic, and seeing
        # which one fired is how a user notices the system read them wrongly.
        # Similarity scores are not shown — they mean nothing to this reader.
        "، ".join(c.term for c in concepts) if concepts else "لم تُطابق أي مصطلحات قانونية",
        content=glossary or None,
    )

    yield StageEvent("retrieval", "started")
    # The last turn's tables are the ones the conversation is about; a follow-up
    # that names none of them would otherwise retrieve past its own subject.
    carry = list(history[-1].table_names) if history else []
    tables = await search_with_carryover(query_vector, carry, concepts)
    if not tables:
        raise PipelineError(NO_TABLES_MESSAGE)
    yield StageEvent("retrieval", "completed", "تم تحديد المعلومات ذات الصلة")

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

    yield StageEvent("prompt", "completed", "تم تجهيز المعلومات اللازمة للإجابة")

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
        raise PipelineError(SQL_REJECTED_MESSAGE) from exc
    yield StageEvent("sql_validation", "completed", "عملية قراءة فقط، دون أي تعديل على البيانات")

    async for event in _execute_and_answer(
        question,
        safe_sql,
        tables,
        schema_context,
        glossary,
        history,
        concepts,
        # First turns only, whatever the read rule allows: an answer produced
        # with a conversation behind it may mean something its wording alone does
        # not, and storing it would put a context-dependent query where a later,
        # unrelated conversation could pick it up.
        #
        # `has_prior_turns`, not `not history` — the two differ whenever
        # CONTEXT_MAX_TURNS <= 0, and the difference was a silent store-everything.
        store=QUERY_CACHE_ENABLED and not has_prior_turns,
    ):
        yield event
