"""LLM review gate for schema changes before they reach Qdrant.

This is the decision-maker, not a second opinion on top of heuristics: for
every changed table the model sees the structure *and a sample of the real
rows*, plus the descriptions of everything already indexed as the definition
of the domain, and answers one question — does a human need to look at this?

Two prompt modes come off the same packet:

- No description yet (a brand-new table): "is this intelligible and in-domain,
  and draft a description for it".
- Description present (the re-check after a user edits): "does this description
  accurately describe these sample rows?". Phrasing the re-check as a claim
  checkable against evidence matters — asked "is this description good?" a
  small instruct model will agree with almost anything a human just wrote.

Failure is always toward review. If LM Studio returns something unparseable
twice, the table is flagged rather than waved through; silently upserting an
unreviewed table is the exact outcome this module exists to prevent.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field

from app.config import CATALOG_REVIEW_CONCURRENCY
from app.services import llm
from app.services.schema_diff import TABLE_DROPPED, TableChange

logger = logging.getLogger(__name__)

SEVERITY_OK = "ok"
SEVERITY_THIN = "thin_description"
SEVERITY_OFF_DOMAIN = "off_domain"
SEVERITY_UNINTELLIGIBLE = "unintelligible"
_SEVERITIES = {SEVERITY_OK, SEVERITY_THIN, SEVERITY_OFF_DOMAIN, SEVERITY_UNINTELLIGIBLE}

_MAX_CELL_CHARS = 200
_MAX_CONTEXT_DESCRIPTION_CHARS = 200
_REVIEW_ATTEMPTS = 2

_SYSTEM_PROMPT = """You are a data catalog reviewer. A database powers an Arabic \
natural-language question answering system: each table has a written description that is \
embedded and searched, so a table with a missing, wrong, or vague description makes the \
whole system answer incorrectly.

Language policy for this catalog — follow it exactly:
- Descriptions are written in ARABIC. Every description you write or judge is Arabic prose.
- Table names, column names and foreign keys are ENGLISH by design. English identifiers are \
CORRECT and must never be treated as a problem. Refer to them verbatim inside the Arabic prose.
- Some columns store English values (for example 'Open', 'Contract Law'). When you describe such \
a column, list the stored values and gloss each one in Arabic in parentheses, e.g. \
"'Open' (مفتوحة)، 'Closed' (مغلقة)". That mapping is what lets the system turn an Arabic question \
into the right SQL filter.

You are shown one table that has just changed, a sample of its real rows, and the \
descriptions of the tables already in the catalog (which define what this database is about).

Decide whether a human needs to review this table before it is indexed. Flag it when:
- the table or column identifiers are not meaningful words in any language (e.g. 'aa_aa_a', \
'tmp2', 'x1'). An ordinary English identifier such as case_number or full_name is not a problem.
- the sample rows have nothing to do with the subject matter of the existing tables
- the existing description does not actually match the sample rows
- an existing description is not in Arabic, or is too vague to tell someone what the \
table holds

Do not flag a table merely for being new, small, or empty, as long as it is intelligible \
and clearly belongs with the others. A table that has NO description yet is the normal case \
for a new table: you are being asked to write one, so its absence is never by itself a reason \
to flag. Judge such a table only on whether it is intelligible and in-domain, and on whether \
you were able to write a real description for it.

Respond with ONLY a JSON object, no prose and no code fences:
{
  "needs_edit": true or false,
  "severity": "ok" | "thin_description" | "off_domain" | "unintelligible",
  "reasons": ["سبب محدد ومختصر بالعربية", ...],
  "suggested_description": "وصف عربي من جملة إلى ثلاث جمل يسمّي الأشياء الواقعية التي يحفظها \
هذا الجدول وعلاقته ببقية الجداول",
  "suggested_column_descriptions": {"column_name": "وصف عربي من سطر واحد", ...}
}

Write "reasons", "suggested_description" and every "suggested_column_descriptions" value in \
ARABIC. Keep the JSON keys and the column names themselves in English.

"reasons" must be empty when needs_edit is false. Always provide \
suggested_description and a suggested_column_descriptions entry for every column."""


@dataclass
class ReviewPacket:
    """Everything the model is shown about one changed table."""

    table_name: str
    change_summary: str
    columns: list[dict] = field(default_factory=list)
    primary_key: str = ""
    foreign_keys: list[dict] = field(default_factory=list)
    sample_rows: list[dict] = field(default_factory=list)
    row_count: int = -1
    current_description: str = ""
    domain_context: list[dict] = field(default_factory=list)


@dataclass
class Verdict:
    needs_edit: bool
    severity: str
    reasons: list[str] = field(default_factory=list)
    suggested_description: str = ""
    suggested_column_descriptions: dict[str, str] = field(default_factory=dict)


def _truncate(value: object, limit: int = _MAX_CELL_CHARS) -> object:
    """Keep a single TEXT/BLOB cell from swallowing the whole prompt."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


def _render_rows(rows: list[dict]) -> str:
    if not rows:
        return "(no rows available)"
    trimmed = [{key: _truncate(value) for key, value in row.items()} for row in rows]
    return json.dumps(trimmed, indent=2, default=str, ensure_ascii=False)


def _render_columns(columns: list[dict]) -> str:
    lines = []
    for column in columns:
        nullability = "NULL" if column.get("nullable") else "NOT NULL"
        line = f"  - {column['name']} ({column['type']}, {nullability})"
        comment = column.get("comment") or ""
        description = column.get("description") or ""
        if description:
            line += f" — current description: {description}"
        elif comment:
            line += f" — MySQL column comment: {comment}"
        else:
            line += " — no description"
        lines.append(line)
    return "\n".join(lines) or "  (none)"


def _render_domain_context(context: list[dict]) -> str:
    if not context:
        return "(the catalog is currently empty)"
    lines = []
    for entry in context:
        description = (entry.get("description") or "")[:_MAX_CONTEXT_DESCRIPTION_CHARS]
        lines.append(f"  - {entry['table_name']}: {description}")
    return "\n".join(lines)


def build_user_prompt(packet: ReviewPacket) -> str:
    foreign_keys = (
        "\n".join(
            f"  - {packet.table_name}.{fk['column']} -> "
            f"{fk['references_table']}.{fk['references_column']}"
            for fk in packet.foreign_keys
        )
        or "  (none — this table is not linked to any other table)"
    )
    row_count = "unknown" if packet.row_count < 0 else str(packet.row_count)

    if packet.current_description:
        question = (
            "This table already has the description below. Read the sample rows and decide "
            "whether that description accurately and specifically describes them. Set "
            '"needs_edit": false only if it does.\n\n'
            f"Current description: {packet.current_description}"
        )
    else:
        # Spelled out because the model otherwise flags the table for the very
        # gap it is being asked to fill. Measured on the `principles` table: it
        # wrote a good Arabic description and all six column descriptions, then
        # set needs_edit with the reason "الوصف مفقود تماماً" — which is about
        # the input, not its own output. Left as it was, every newly created
        # table would be held back and the unattended path would never fire.
        question = (
            "This table has no description yet — that is expected, and is NOT a reason to "
            "flag it. Your job is to write one. Decide whether the table is intelligible and "
            "belongs in this catalog; set \"needs_edit\": true only if it is unintelligible, "
            "off-domain, or you could not write a real description from what you were shown."
        )

    return f"""Tables already in the catalog (this is what the database is about):
{_render_domain_context(packet.domain_context)}

--- Table under review: {packet.table_name} ---

What changed: {packet.change_summary}

Columns:
{_render_columns(packet.columns)}

Primary key: {packet.primary_key or "(none)"}

Foreign keys:
{foreign_keys}

Total rows in table: {row_count}

Sample rows:
{_render_rows(packet.sample_rows)}

--- Your task ---
{question}

JSON verdict:"""


def _fallback_verdict(reason: str, packet: ReviewPacket) -> Verdict:
    """The fail-closed result: flag for a human rather than pass it through."""
    return Verdict(
        needs_edit=True,
        severity=SEVERITY_UNINTELLIGIBLE,
        reasons=[reason],
        suggested_description=packet.current_description,
        suggested_column_descriptions={},
    )


def parse_verdict(payload: dict) -> Verdict:
    """Coerce a model response into a Verdict, tolerating loose shapes.

    `needs_edit` is authoritative; severity is normalized to the allowed set
    rather than trusted, because a small model will occasionally invent one.
    """
    needs_edit = bool(payload.get("needs_edit", True))

    severity = str(payload.get("severity", "")).strip().lower()
    if severity not in _SEVERITIES:
        severity = SEVERITY_THIN if needs_edit else SEVERITY_OK

    raw_reasons = payload.get("reasons") or []
    if isinstance(raw_reasons, str):
        raw_reasons = [raw_reasons]
    reasons = [str(reason).strip() for reason in raw_reasons if str(reason).strip()]

    raw_columns = payload.get("suggested_column_descriptions") or {}
    column_descriptions = (
        {str(name): str(text) for name, text in raw_columns.items()}
        if isinstance(raw_columns, dict)
        else {}
    )

    if needs_edit and not reasons:
        reasons = ["The reviewer flagged this table but gave no specific reason."]

    return Verdict(
        needs_edit=needs_edit,
        severity=severity,
        reasons=[] if not needs_edit else reasons,
        suggested_description=str(payload.get("suggested_description") or "").strip(),
        suggested_column_descriptions=column_descriptions,
    )


async def review(packet: ReviewPacket) -> Verdict:
    """Ask the model whether this table needs a human. Never raises."""
    user_prompt = build_user_prompt(packet)

    for attempt in range(1, _REVIEW_ATTEMPTS + 1):
        try:
            payload = await llm.chat_json(_SYSTEM_PROMPT, user_prompt)
            return parse_verdict(payload)
        except ValueError as exc:
            logger.warning(
                "Review of %s returned unusable JSON (attempt %d/%d): %s",
                packet.table_name,
                attempt,
                _REVIEW_ATTEMPTS,
                exc,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced to the user as a flag, not a 500
            logger.warning("Review of %s failed (attempt %d/%d): %s", packet.table_name, attempt, _REVIEW_ATTEMPTS, exc)

    return _fallback_verdict(
        "Automated review did not return a usable verdict — please check this table by hand.",
        packet,
    )


async def review_many(packets: list[ReviewPacket]) -> list[Verdict]:
    """Review several tables concurrently, bounded so live queries keep working."""
    semaphore = asyncio.Semaphore(max(1, CATALOG_REVIEW_CONCURRENCY))

    async def _one(packet: ReviewPacket) -> Verdict:
        async with semaphore:
            return await review(packet)

    return list(await asyncio.gather(*(_one(packet) for packet in packets)))


def needs_review(change: TableChange) -> bool:
    """Dropped tables are a mechanical delete — there is nothing to judge.

    They still require approval, but spending an LLM call on a table that no
    longer exists (and therefore has no rows to sample) buys nothing.
    """
    return change.change_type != TABLE_DROPPED
