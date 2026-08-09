"""Document new and changed tables automatically, off Debezium change events.

Debezium Server tails the MySQL binlog and POSTs every change event to
/api/cdc/events. This module turns that stream into documented tables in Qdrant,
so a table created in the SQL editor becomes queryable in the chatbot without
anyone running a scan.

Debezium is the trigger, not the engine
---------------------------------------
A change event says a table was created or written to. It does not carry the
sample rows the reviewer needs, and its column metadata is a second copy of
something `information_schema` already has. So an event only marks a table as
worth looking at; the actual work runs the same
introspect → diff_schema → reviewer path the manual Scan button uses, via
services/documenter.py. That way both paths produce identical documents.

Why events are debounced
------------------------
A CREATE TABLE reaches us the instant it commits — when the table is still
empty. Sample rows are the reviewer's strongest evidence for telling a real
domain table apart from somebody's scratch import, so documenting at DDL time
means judging with no evidence at all.

Instead a table waits until its writes go quiet (CDC_DEBOUNCE_SECONDS), capped
by CDC_MAX_WAIT_SECONDS so a long bulk load cannot postpone it forever. A
CREATE followed by an INSERT loop therefore fires once, after the load, with
rows to look at. A CREATE with no inserts fires on the timeout with zero rows,
which is itself a signal the reviewer already knows how to use.

What happens to the verdict
---------------------------
Clean verdict → embedded and upserted straight into schema_docs, unattended.
Flagged verdict → held back, not indexed, and raised as a warning in the
Activity tab.

Not indexing a flagged table is the one place the automated path deliberately
stops. The manual flow's rule that a human's text is authoritative still holds,
and a description the model itself could not vouch for is not something to index
silently.

BOTH outcomes land in services/review_queue.py, so the Schema updates tab can
show what was written and let it be corrected. A clean verdict means "not
obviously wrong", not "well written" — and once a table is indexed, a scan can
never surface it again, because a scan reports tables that DIFFER from what is
indexed and this one matches by definition.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.config import (
    CDC_DEBOUNCE_SECONDS,
    CDC_ENABLED,
    CDC_MAX_WAIT_SECONDS,
    MYSQL_DATABASE,
)
from app.services import activity, documenter, introspect, review_queue, reviewer
from app.services.catalog import (
    build_doc_text,
    delete_tables,
    ensure_collection,
    fetch_indexed_tables,
    upsert_tables,
)
from app.services.embeddings import embed_texts
from app.services.schema_diff import TABLE_DROPPED, diff_schema

logger = logging.getLogger(__name__)

SIGNAL_DDL = "ddl"
SIGNAL_ROW = "row"

# How often the worker checks whether anything is due. The debounce window is
# measured in tens of seconds, so a one-second tick is far finer than needed and
# costs nothing.
_TICK_SECONDS = 1.0


@dataclass
class TableSignal:
    table_name: str
    kind: str
    ddl: str = ""


# --- Event parsing (pure) -----------------------------------------------------


def _unwrap(event: dict) -> dict:
    """Strip whichever envelope Debezium's output format put around the event.

    Three shapes reach the sink depending on how the connector is configured, and
    the parser accepts all of them rather than pinning one — the format is set in
    a properties file that is easy to change without anyone remembering this code
    depends on it.

    - `{"schema": ..., "payload": {...}}`  when schemas are enabled
    - `{"data": {...}}`                    when CloudEvents wrapping is on
    - `{...}`                              plain JSON, the configured default
    """
    seen = 0
    while isinstance(event, dict) and seen < 4:
        if isinstance(event.get("payload"), dict):
            event = event["payload"]
        elif isinstance(event.get("data"), dict):
            event = event["data"]
        else:
            break
        seen += 1
    return event if isinstance(event, dict) else {}


def _table_from_qualified_id(qualified: str) -> str:
    """`"legal_db"."cases"` -> `cases`.

    Debezium quotes each part with the source database's identifier quoting, so
    the quote character varies. Splitting on the dot and stripping quote
    characters covers MySQL's backticks and the double quotes it also emits.
    """
    parts = [part.strip().strip('"`\'') for part in qualified.split(".")]
    parts = [part for part in parts if part]
    return parts[-1] if parts else ""


def parse_event(event: dict) -> list[TableSignal]:
    """Extract the tables one Debezium event touches.

    Returns [] for anything not about this database — heartbeats, transaction
    markers, and events from other schemas all arrive on the same sink.
    """
    event = _unwrap(event)
    if not event:
        return []

    signals: list[TableSignal] = []

    # A schema-change event. `tableChanges` is the parsed form of the DDL and is
    # what makes CREATE/ALTER/DROP distinguishable without parsing SQL here.
    table_changes = event.get("tableChanges")
    if isinstance(table_changes, list):
        for change in table_changes:
            if not isinstance(change, dict):
                continue
            table_name = _table_from_qualified_id(str(change.get("id", "")))
            if not table_name:
                table = change.get("table")
                if isinstance(table, dict):
                    table_name = str(table.get("id", "")) or ""
            if table_name:
                signals.append(
                    TableSignal(
                        table_name=table_name,
                        kind=SIGNAL_DDL,
                        ddl=str(event.get("ddl", ""))[:2000],
                    )
                )
        if signals:
            return signals

    # A row-level event.
    source = event.get("source")
    if isinstance(source, dict):
        database = source.get("db") or source.get("database") or ""
        table_name = source.get("table") or ""
        # Debezium reports the database it read the event from; events for other
        # schemas on the same server are not ours to document.
        if table_name and (not database or database == MYSQL_DATABASE):
            signals.append(TableSignal(table_name=str(table_name), kind=SIGNAL_ROW))

    return signals


def parse_body(body: object) -> list[TableSignal]:
    """Parse whatever the HTTP sink posted: one event, or a batch of them."""
    if isinstance(body, list):
        signals: list[TableSignal] = []
        for item in body:
            if isinstance(item, dict):
                signals.extend(parse_event(item))
        return signals
    if isinstance(body, dict):
        return parse_event(body)
    return []


# --- Debounce buffer ----------------------------------------------------------


@dataclass
class PendingTable:
    table_name: str
    first_seen: float
    last_event: float
    saw_ddl: bool = False
    saw_rows: bool = False
    event_count: int = 0

    def is_due(self, now: float) -> bool:
        """Quiet long enough, or waited long enough. See the module docstring."""
        quiet_for = now - self.last_event
        waited_for = now - self.first_seen
        return quiet_for >= CDC_DEBOUNCE_SECONDS or waited_for >= CDC_MAX_WAIT_SECONDS

    def trigger_summary(self) -> str:
        if self.saw_ddl and self.saw_rows:
            return "schema change followed by row writes"
        if self.saw_ddl:
            return "schema change"
        return "row writes"


class CdcBuffer:
    """Collects signals per table and reports which have settled."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingTable] = {}

    def add(self, signal: TableSignal, now: float | None = None) -> None:
        now = time.time() if now is None else now
        entry = self._pending.get(signal.table_name)
        if entry is None:
            entry = PendingTable(
                table_name=signal.table_name, first_seen=now, last_event=now
            )
            self._pending[signal.table_name] = entry

        entry.last_event = now
        entry.event_count += 1
        if signal.kind == SIGNAL_DDL:
            entry.saw_ddl = True
        else:
            entry.saw_rows = True

    def take_due(self, now: float | None = None) -> list[PendingTable]:
        """Remove and return every table whose window has closed."""
        now = time.time() if now is None else now
        due = [entry for entry in self._pending.values() if entry.is_due(now)]
        for entry in due:
            self._pending.pop(entry.table_name, None)
        return due

    def pending_tables(self) -> list[str]:
        return sorted(self._pending)

    def clear(self) -> None:
        self._pending.clear()


# --- Documenting --------------------------------------------------------------


async def document_tables(table_names: list[str], trigger: str) -> None:
    """Document every named table that actually differs from what is indexed.

    Introspection and the indexed-document fetch happen once for the whole batch:
    a bulk migration creating six tables produces six due entries at the same
    tick, and re-reading information_schema per table would be six times the work
    for the same answer.
    """
    if not table_names:
        return

    try:
        live = await introspect.introspect_schema()
        indexed = await fetch_indexed_tables()
    except Exception as exc:  # noqa: BLE001
        logger.exception("CDC could not read the current schema")
        activity.record(
            activity.SOURCE_CDC,
            "documentation_failed",
            f"تعذّرت قراءة بنية الجداول لتوثيق {'، '.join(table_names)}: {exc}",
            level=activity.LEVEL_ERROR,
            tables=table_names,
        )
        return

    wanted = set(table_names)
    changes = [change for change in diff_schema(live, indexed) if change.table_name in wanted]

    settled = wanted - {change.table_name for change in changes}
    if settled:
        # The event was real but the document already matches — someone ran the
        # manual scan first, or a write touched a table whose structure is
        # unchanged. Nothing to do and nothing worth logging as an event.
        logger.info("CDC: %s already documented and unchanged", ", ".join(sorted(settled)))

    known_tables = set(live)
    for change in changes:
        try:
            await _document_one(change, indexed, known_tables, trigger)
        except Exception as exc:  # noqa: BLE001 — one bad table must not stop the batch
            logger.exception("CDC failed to document %s", change.table_name)
            activity.record(
                activity.SOURCE_CDC,
                "documentation_failed",
                f"تعذّر توثيق الجدول '{change.table_name}': {exc}",
                level=activity.LEVEL_ERROR,
                table_name=change.table_name,
            )


async def _document_one(change, indexed: dict[str, dict], known_tables: set[str], trigger: str) -> None:
    if change.change_type == TABLE_DROPPED:
        # The table is gone from MySQL, so its document describes something that
        # no longer exists — leaving it indexed makes the model write SQL against
        # a missing table. Deleting it needs no judgement and gets none.
        await delete_tables([change.table_name])
        review_queue.remove(change.table_name)
        activity.record(
            activity.SOURCE_CDC,
            "table_removed",
            f"حُذف الجدول '{change.table_name}' من قاعدة البيانات — وأُزيل وصفه من الفهرس.",
            level=activity.LEVEL_INFO,
            table_name=change.table_name,
            trigger=trigger,
        )
        return

    packet = await documenter.build_packet(change, indexed, known_tables)
    verdict = await reviewer.review(packet)
    doc = documenter.build_doc(change, packet, verdict)

    if verdict.needs_edit or not doc.description.strip():
        # A clean verdict with no description is still unusable: the description
        # is what retrieval matches a question against, so indexing an empty one
        # adds a table the chatbot can never find.
        reason = (
            "; ".join(verdict.reasons)
            if verdict.reasons
            else "the reviewer produced no usable description"
        )
        review_queue.add(
            table_name=change.table_name,
            change_type=change.change_type,
            summary=change.summary(),
            doc=doc.model_dump(),
            verdict=vars(verdict),
            sample_rows=packet.sample_rows,
            row_count=packet.row_count,
            indexed=False,
        )
        activity.record(
            activity.SOURCE_CDC,
            "documentation_flagged",
            f"تم توثيق الجدول '{change.table_name}' لكنه يحتاج مراجعة بشرية: {reason}",
            level=activity.LEVEL_WARNING,
            table_name=change.table_name,
            severity=verdict.severity,
            reasons=verdict.reasons,
            suggested_description=verdict.suggested_description,
            summary=change.summary(),
            row_count=packet.row_count,
            trigger=trigger,
        )
        return

    payload = doc.model_dump()
    await ensure_collection()
    vectors = await embed_texts([build_doc_text(payload)])
    await upsert_tables([payload], vectors)

    # Queued even though it is already queryable. A clean verdict means "not
    # obviously wrong", not "well written" — and nobody has read this text. A
    # scan will never surface it again either, because the table now matches
    # what is indexed by definition. So this is the only route back to a
    # description the model wrote unattended.
    review_queue.add(
        table_name=change.table_name,
        change_type=change.change_type,
        summary=change.summary(),
        doc=payload,
        verdict=vars(verdict),
        sample_rows=packet.sample_rows,
        row_count=packet.row_count,
        indexed=True,
    )

    activity.record(
        activity.SOURCE_CDC,
        "table_documented",
        f"تم توثيق الجدول '{change.table_name}' تلقائيًا وأصبح قابلًا للاستعلام.",
        level=activity.LEVEL_INFO,
        table_name=change.table_name,
        change_type=change.change_type,
        summary=change.summary(),
        description=doc.description,
        column_count=len(doc.columns),
        # The per-column descriptions, not just how many there were. This is
        # what the model actually wrote and what retrieval will match against,
        # so it is the thing worth reading when checking its work — a count
        # tells you nothing about whether the columns were described well.
        columns=[
            {"name": column.name, "description": column.description} for column in doc.columns
        ],
        row_count=packet.row_count,
        trigger=trigger,
    )


# --- The running service ------------------------------------------------------


class CdcService:
    def __init__(self) -> None:
        self.buffer = CdcBuffer()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._events_received = 0

    async def start(self) -> None:
        if not CDC_ENABLED:
            logger.info("CDC intake disabled (CDC_ENABLED=false)")
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="cdc-worker")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutting down
                pass
            self._task = None

    def ingest(self, body: object) -> int:
        """Take one HTTP post from the Debezium sink. Returns signals accepted."""
        signals = parse_body(body)
        for signal in signals:
            self.buffer.add(signal)
        self._events_received += 1
        return len(signals)

    def status(self) -> dict:
        return {
            "enabled": CDC_ENABLED,
            "running": self._task is not None and not self._task.done(),
            "events_received": self._events_received,
            "pending_tables": self.buffer.pending_tables(),
            "debounce_seconds": CDC_DEBOUNCE_SECONDS,
        }

    async def _run(self) -> None:
        while not self._stopping:
            await asyncio.sleep(_TICK_SECONDS)
            try:
                due = self.buffer.take_due()
                if not due:
                    continue
                trigger = ", ".join(sorted({entry.trigger_summary() for entry in due}))
                await document_tables([entry.table_name for entry in due], trigger=trigger)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the worker must outlive any one failure
                logger.exception("CDC worker tick failed")


service = CdcService()
