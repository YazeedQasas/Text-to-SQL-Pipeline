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
from app.services import context

# These stay written in English even though the system is Arabic-facing: they are
# instructions *about* SQL, and instruction-following on code generation is
# noticeably more reliable in English. Only the parts that describe Arabic input
# and Arabic output are language-specific.
_SQL_SYSTEM_PROMPT = """You are a MySQL expert. Given a database schema and a user's question, \
write a single read-only SQL query that answers the question.

The user asks in Arabic and the schema descriptions are written in Arabic, but every table and \
column identifier is English. Use the English identifiers exactly as they appear in the schema.

Rules:
- Output ONLY the SQL query. No explanation, no markdown code fences, no comments.
- Use only the tables and columns given in the schema below. Never invent column or table names.
- Only ever write a SELECT statement. Never write INSERT, UPDATE, DELETE, DROP, ALTER, or any other \
statement.
- When the question spans multiple tables, use JOINs based on the relationships listed below.
- Prefer explicit column names over SELECT *.
- Category and ENUM columns (status, role, party_type, level, jurisdiction, area_of_law, \
case_type, verdict, hearing_type) accept only a fixed set of values. Each such column's \
description lists those values verbatim. Copy the value EXACTLY as written there, character for \
character — a near-miss silently returns zero rows.
- Always match names and titles with a LIKE on the core name and wildcards, never with equality.
- Some tables carry a normalized twin column for names and titles, whose name ends in _norm. Look \
at the column list of the table you are querying: if such a column IS listed there, match against \
it rather than the raw column, because the raw column keeps spelling variants that will not match. \
If NO column ending in _norm is listed for that table, match the raw column — never write a _norm \
column that does not appear in the schema below.
- When you do match against a _norm column, normalize the search term the same way: bare alef ا \
(never أ إ آ), yaa ي (never ى), haa ه (never ة), and no diacritics — so "إيمان مصطفى" is written \
as '%ايمان مصطفي%'. Keep the raw column in the SELECT list for display; a _norm column is for \
matching only.
- Strip honorifics and titles from a name before matching, in either language: Arabic ones such \
as القاضي / القاضية / المحامي / المحامية / السيد / السيدة / الدكتور / الدكتورة / الأستاذ / معالي / سعادة, \
and English ones such as "Judge", "Mr.", "Ms.", "Dr.". They are not part of the stored name.
- Reference codes (case_number, bar_number) are stored in Latin script — match those literally, \
without normalization.
- Check the script the data itself is in. If the column description or sample values show that \
names are stored in Latin script while the question writes the name in Arabic script, match on the \
transliterated Latin form of the core name (فلان أوكافور -> '%Okafor%'), not on the Arabic \
spelling — an Arabic search term can never match a Latin-script value.
- Read the whole schema before deciding a question is unanswerable. A question can be about the \
subject without using the schema's wording: a category the user names in their own words is \
often one of the fixed values listed in a column description, and a property they ask for is \
often derivable from the columns that ARE there (a duration from two dates, "busiest" from a \
count). Reach for those before giving up.
- Only when no combination of the tables above can answer the question, output NO_QUERY followed \
by a colon and one short Arabic sentence saying what is missing, addressed to the user — for \
example: NO_QUERY: لا تتضمن البيانات معلومات عن الطقس. Write nothing else on that line, and \
never use NO_QUERY for a question the schema can answer even partially.
"""

_ANSWER_SYSTEM_PROMPT = """You are a helpful legal-research assistant. You are given the user's \
original question, the SQL query that was run, and the resulting rows. Write a concise, direct \
natural-language answer to the question using only the data in the results.

Always answer in Modern Standard Arabic, whatever language the underlying data is in.

Rules:
- Write PLAIN TEXT only. No Markdown whatsoever: no asterisks for bold or bullets, no #, no \
backticks, no tables. The answer is rendered as-is in a right-to-left Arabic page, so Markdown \
syntax appears literally as punctuation the reader has to look past.
- One or two rows: answer in a sentence. Three or more: write one line per row, each starting \
with "- ", with the fields separated by " — " and the most identifying field first, like:
- MS-2026-0301 — النيابة العامة ضد سامي الحديد — 2026-06-03 — مفتوحة
Introduce the list with a short sentence and do not repeat the field names on every line; if the \
fields need naming, name them once in that sentence.
- If the results are empty, say so plainly in Arabic instead of guessing.
- Do not mention SQL, tables, or columns by name unless the user asked about the schema itself.
- Reproduce identifiers exactly as they are stored — case numbers, reference codes, and names \
written in Latin script are kept verbatim, because that is how the user will find them in the \
record. You may add an Arabic gloss in parentheses after a term you translate.
- Use Western digits (0-9) rather than Arabic-Indic numerals, so dates and case numbers match the \
stored records.
"""


async def _chat_stream(messages: list[dict]) -> AsyncIterator[str]:
    """Yield content deltas from an OpenAI-compatible streaming chat completion."""
    async with httpx.AsyncClient(base_url=LM_STUDIO_BASE_URL, timeout=120.0) as client:
        async with client.stream(
            "POST",
            "/chat/completions",
            json={
                "model": LLM_MODEL,
                "messages": messages,
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


# Shown when the model refuses without saying why (it drops the reason often
# enough that the user must not be handed a bare "NO_QUERY").
NO_QUERY_FALLBACK = "لا يمكن الإجابة عن هذا السؤال من البيانات المتاحة."

_NO_QUERY = re.compile(r"^\W*NO_QUERY\b[\s:،-]*(.*)", re.IGNORECASE | re.DOTALL)


def parse_no_query(sql: str) -> str | None:
    """Return the refusal reason if this response is a refusal, else None.

    The refusal is decided at SQL generation rather than before retrieval,
    because this is the first step that sees the actual columns and their
    permitted values — the table descriptions alone are too coarse to tell an
    off-topic question apart from one that words a real category differently.

    Only a response that *starts* with NO_QUERY counts, so a query that happens
    to mention the token in a string literal still runs.
    """
    match = _NO_QUERY.match(sql.strip())
    if match is None:
        return None
    return match.group(1).strip().strip('"') or NO_QUERY_FALLBACK


def build_sql_messages(
    question: str, schema_context: str, history: list = (), glossary: str = ""
) -> list[dict]:
    """Prompt for SQL generation, with past turns replayed as question → SQL.

    Past turns carry the SQL rather than the answer: the useful context for
    writing a query is the query that was written last time, which the model can
    edit ("only the open ones") instead of composing from scratch. The schema
    rides on the final message only — it is rebuilt from retrieval every turn,
    so repeating it per historical turn would pay for it many times over.

    `glossary` is the rendered domain-term block (retrieval.py), empty on the
    usual turn. It sits AFTER the schema and immediately before the question for
    two reasons: it is meaningless without the columns it references, and LM
    Studio truncates an overlong prompt from the START — so the further from the
    head this sits, the later it is lost. Its instructions live inside the block
    rather than in _SQL_SYSTEM_PROMPT so that a turn matching no concepts pays
    nothing for the feature.
    """
    messages = [{"role": "system", "content": _SQL_SYSTEM_PROMPT}]

    for turn in history:
        if not turn.sql:
            continue  # a turn that never produced SQL teaches nothing here
        messages.append({"role": "user", "content": turn.question})
        messages.append({"role": "assistant", "content": turn.sql})

    glossary_section = f"{glossary}\n\n" if glossary else ""
    messages.append(
        {
            "role": "user",
            "content": (
                f"Schema:\n{schema_context}\n\n"
                f"{glossary_section}"
                f"Question: {question}\n\nSQL query:"
            ),
        }
    )
    return messages


_MAX_PREVIEW_ROWS = 50


def build_answer_messages(
    question: str, sql: str, columns: list[str], rows: list[dict], history: list = ()
) -> list[dict]:
    """Prompt for the natural-language answer, with past question/answer pairs.

    Only the prose of past turns is replayed, never their result rows — one
    200-row result costs more than the entire rest of the conversation.

    This turn's rows are fitted to whatever budget is left rather than to a
    fixed count, because rows are the one input whose size the user controls:
    a wide SELECT over 50 rows can outweigh the schema and the whole transcript
    on its own. Overflowing here would truncate the head of the prompt — the
    instruction to answer in Arabic and the question itself — and the model
    would answer something else entirely.
    """
    messages = [{"role": "system", "content": _ANSWER_SYSTEM_PROMPT}]

    for turn in history:
        if not turn.answer:
            continue
        messages.append({"role": "user", "content": turn.question})
        messages.append({"role": "assistant", "content": turn.answer})

    def render(preview: list[dict]) -> str:
        shown = f", showing {len(preview)}" if len(preview) < len(rows) else ""
        return (
            f"Question: {question}\n\n"
            f"SQL executed: {sql}\n\n"
            f"Columns: {columns}\n\n"
            f"Rows ({len(rows)} total{shown}): {preview}\n\n"
            "Answer:"
        )

    # Halve the preview until it fits. Starting from the cap and stepping down
    # keeps the common case (small results) at a single measurement.
    preview_rows = rows[:_MAX_PREVIEW_ROWS]
    headroom = context.limit_tokens() - context.estimate_messages(messages)
    while preview_rows and context.estimate_tokens(render(preview_rows)) > headroom:
        preview_rows = preview_rows[: len(preview_rows) // 2]

    messages.append({"role": "user", "content": render(preview_rows)})
    return messages


def stream_sql(
    question: str, schema_context: str, history: list = (), glossary: str = ""
) -> AsyncIterator[str]:
    return _chat_stream(build_sql_messages(question, schema_context, history, glossary))


def stream_answer(
    question: str, sql: str, columns: list[str], rows: list[dict], history: list = ()
) -> AsyncIterator[str]:
    return _chat_stream(build_answer_messages(question, sql, columns, rows, history))


async def collect(stream: AsyncIterator[str]) -> str:
    return "".join([chunk async for chunk in stream])


def _extract_json_object(text: str) -> str:
    """Pull the outermost JSON object out of a model response.

    Small instruct models routinely wrap JSON in prose or code fences even when
    told not to, so this scans for the first balanced {...} rather than
    trusting the response to be clean.
    """
    unfenced = strip_code_fences(text)
    start = unfenced.find("{")
    if start == -1:
        raise ValueError("No JSON object found in model response.")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(unfenced)):
        char = unfenced[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return unfenced[start : index + 1]

    raise ValueError("Unterminated JSON object in model response.")


async def chat_json(system_prompt: str, user_prompt: str) -> dict:
    """Run a non-streaming completion and parse the reply as a JSON object.

    Reuses the streaming transport rather than adding a second HTTP shape to
    this module — the caller just wants the finished text. Raises ValueError if
    the response is not usable JSON; callers decide the retry and fallback
    policy.
    """
    raw = await collect(
        _chat_stream(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
    )
    parsed = json.loads(_extract_json_object(raw))
    if not isinstance(parsed, dict):
        raise ValueError("Model response was valid JSON but not an object.")
    return parsed
