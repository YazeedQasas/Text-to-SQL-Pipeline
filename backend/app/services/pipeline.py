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

from app.models import QueryResponse, RetrievedTable
from app.services import llm
from app.services.db import execute_select
from app.services.embeddings import embed_text
from app.services.retrieval import format_tables_for_prompt, search_tables
from app.services.sql_guard import SQLGuardError, enforce_select_only

logger = logging.getLogger(__name__)

# The ordered stage list is sent to the client before the run starts, so the UI
# can render the whole progress list (including not-yet-reached steps) without
# duplicating this definition in the frontend.
STAGES: list[dict[str, str]] = [
    {"id": "embedding", "label": "Embedding your question"},
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


PipelineEvent = StageEvent | TokenEvent | QueryResponse


async def run_pipeline(question: str) -> AsyncIterator[PipelineEvent]:
    yield StageEvent("embedding", "started")
    query_vector = await embed_text(question)
    yield StageEvent("embedding", "completed", f"{len(query_vector)}-dimension vector")

    yield StageEvent("retrieval", "started")
    tables = await search_tables(query_vector)
    if not tables:
        raise PipelineError("No relevant tables found for this question.")
    yield StageEvent(
        "retrieval",
        "completed",
        f"{len(tables)} tables: " + ", ".join(t.table_name for t in tables),
    )

    yield StageEvent("prompt", "started")
    schema_context = format_tables_for_prompt(tables)
    yield StageEvent(
        "prompt",
        "completed",
        f"{len(schema_context)} characters of schema context",
        content=schema_context,
    )

    yield StageEvent("sql_generation", "started")
    sql_chunks: list[str] = []
    async for chunk in llm.stream_sql(question, schema_context):
        sql_chunks.append(chunk)
        yield TokenEvent("sql_generation", chunk)
    raw_sql = llm.strip_code_fences("".join(sql_chunks))
    if raw_sql.strip().upper() == "NO_QUERY":
        raise PipelineError("The question could not be answered with the available schema.")
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
    async for chunk in llm.stream_answer(question, safe_sql, columns, rows):
        answer_chunks.append(chunk)
        yield TokenEvent("answer_generation", chunk)
    answer = "".join(answer_chunks).strip()
    yield StageEvent("answer_generation", "completed")

    yield QueryResponse(
        answer=answer,
        sql=safe_sql,
        columns=columns,
        rows=rows,
        retrieved_tables=[
            RetrievedTable(table_name=t.table_name, description=t.description, score=t.score)
            for t in tables
        ],
    )
