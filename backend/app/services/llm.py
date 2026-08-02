"""Gemma 4 E4B client (served via LM Studio's OpenAI-compatible API) for SQL
generation and answer formatting.

Both calls are exposed as token streams so the pipeline can forward partial
output to the client while the model is still writing. Callers that just want
the finished string accumulate the stream themselves.
"""

import json
import re
from collections.abc import AsyncIterator

import httpx

from app.config import LLM_MODEL, LM_STUDIO_BASE_URL

_SQL_SYSTEM_PROMPT = """You are a MySQL expert. Given a database schema and a user's question, \
write a single read-only SQL query that answers the question.

Rules:
- Output ONLY the SQL query. No explanation, no markdown code fences, no comments.
- Use only the tables and columns given in the schema below. Never invent column or table names.
- Only ever write a SELECT statement. Never write INSERT, UPDATE, DELETE, DROP, ALTER, or any other \
statement.
- When the question spans multiple tables, use JOINs based on the relationships listed below.
- Prefer explicit column names over SELECT *.
- When matching a person's name (judges, lawyers, parties), use a case-insensitive LIKE with \
wildcards on the core name (e.g. full_name LIKE '%Amara Okafor%'), not exact equality — strip \
titles/honorifics like "Judge", "Mr.", "Ms.", "Dr." from the search term first, since they are \
not part of the stored name.
- If the question cannot be answered with the given schema, output exactly: NO_QUERY
"""

_ANSWER_SYSTEM_PROMPT = """You are a helpful legal-research assistant. You are given the user's \
original question, the SQL query that was run, and the resulting rows. Write a concise, direct \
natural-language answer to the question using only the data in the results. If the results are \
empty, say so plainly instead of guessing. Do not mention SQL, tables, or columns by name unless \
the user asked about the schema itself.
"""


async def _chat_stream(system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
    """Yield content deltas from an OpenAI-compatible streaming chat completion."""
    async with httpx.AsyncClient(base_url=LM_STUDIO_BASE_URL, timeout=120.0) as client:
        async with client.stream(
            "POST",
            "/chat/completions",
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": True,
                "temperature": 0.1,
            },
        ) as response:
            if response.status_code >= 400:
                await response.aread()  # streamed responses must be read before .text is usable
                response.raise_for_status()

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue  # keep-alive or non-JSON line
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {}).get("content")
                if delta:
                    yield delta


def strip_code_fences(text: str) -> str:
    match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else text.strip()


def stream_sql(question: str, schema_context: str) -> AsyncIterator[str]:
    user_prompt = f"Schema:\n{schema_context}\n\nQuestion: {question}\n\nSQL query:"
    return _chat_stream(_SQL_SYSTEM_PROMPT, user_prompt)


def stream_answer(
    question: str, sql: str, columns: list[str], rows: list[dict]
) -> AsyncIterator[str]:
    preview_rows = rows[:50]  # keep the prompt bounded even if MAX_RESULT_ROWS is large
    user_prompt = (
        f"Question: {question}\n\n"
        f"SQL executed: {sql}\n\n"
        f"Columns: {columns}\n\n"
        f"Rows ({len(rows)} total, showing up to 50): {preview_rows}\n\n"
        "Answer:"
    )
    return _chat_stream(_ANSWER_SYSTEM_PROMPT, user_prompt)


async def collect(stream: AsyncIterator[str]) -> str:
    return "".join([chunk async for chunk in stream])
