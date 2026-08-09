"""Notice deletions made directly in Qdrant, by tailing its container logs.

Qdrant runs locally in Docker and has no change feed, no webhooks and no
triggers. When someone deletes points through the dashboard or with a raw API
call, nothing tells the application. The container log is the only signal
available, so this tails it.

What the log actually contains
------------------------------
Measured against qdrant:v1.9.4 by creating a collection, inserting two points,
deleting one, and dropping the collection:

    INFO ...collection_meta_ops: Creating collection zz_probe_scratch
    INFO actix_web...logger: "PUT /collections/zz_probe_scratch HTTP/1.1" 200
    INFO actix_web...logger: "PUT /collections/zz_probe_scratch/points?wait=true" 200
    INFO actix_web...logger: "POST /collections/zz_probe_scratch/points/delete?wait=true" 200
    INFO ...collection_meta_ops: Deleting collection zz_probe_scratch

A collection drop gets a named line. A point delete gets an HTTP access line
only — and the access log does NOT include the request body, so the point ids
are not in there and cannot be recovered from it.

That is why this module is a TRIGGER and never a source of truth: it reports
that *something* was deleted from the concepts collection, and concept_sync
works out what by comparing the collection against the file. The same design
means a deletion made through the Qdrant dashboard is caught identically to one
made through the API, because both are the same REST call.

Known blind spot: only the REST API (6333) goes through actix's access logger.
A delete issued over gRPC (6334) produces no line at all. qdrant-client defaults
to REST so the application's own writes are covered, and the periodic sync is
the backstop for anything that is not.
"""

import asyncio
import logging
import re

from app.config import (
    QDRANT_COLLECTION,
    QDRANT_CONTAINER,
    QDRANT_WATCH_DEBOUNCE_SECONDS,
    QDRANT_WATCH_ENABLED,
    QDRANT_CONCEPTS_COLLECTION,
)
from app.services import activity, concept_sync

logger = logging.getLogger(__name__)

DELETE_POINTS = "points_deleted"
DELETE_COLLECTION = "collection_deleted"

# An access-log line for a delete endpoint that returned 2xx. A 4xx changed
# nothing, so reconciling after it would be pure noise.
_ACCESS_DELETE_RE = re.compile(
    r'"(?:POST|PUT|DELETE)\s+/collections/(?P<collection>[^/\s?"]+)'
    r'(?P<path>/points(?:/vectors)?/delete|/points/delete|)\S*\s+HTTP/[\d.]+"\s+2\d\d'
)
# Qdrant's own line for a collection being dropped, which does name it.
_DROP_COLLECTION_RE = re.compile(r"Deleting collection\s+(?P<collection>\S+)")

# How long to wait before retrying a watcher whose `docker logs` process died.
_RESTART_DELAY_SECONDS = 10.0


def parse_delete_signal(line: str, collection: str) -> str | None:
    """Classify one log line as a deletion affecting `collection`, or not.

    Pure, so the log shapes above can be tested without a container.
    """
    kind, name = classify_delete(line)
    return kind if name == collection else None


def classify_delete(line: str) -> tuple[str | None, str | None]:
    """Return (kind, collection) for a deletion log line, or (None, None).

    Split out from parse_delete_signal because two collections are watched now
    and they are handled differently — see the watcher's docstring.
    """
    match = _DROP_COLLECTION_RE.search(line)
    if match:
        return DELETE_COLLECTION, match.group("collection")

    match = _ACCESS_DELETE_RE.search(line)
    if match:
        # A bare `DELETE /collections/<name>` drops the whole collection; the
        # `/points/delete` variants remove points from it.
        if match.group("path"):
            return DELETE_POINTS, match.group("collection")
        if '"DELETE ' in line:
            return DELETE_COLLECTION, match.group("collection")
    return None, None


class QdrantWatcher:
    """Tails `docker logs -f` and reacts to deletions in either collection.

    Deletions are coalesced rather than acted on individually: deleting twelve
    points from the dashboard arrives as one line, but a script deleting them
    one at a time arrives as twelve, and reacting to each would re-embed the
    file a dozen times over.

    The two collections are handled differently because only one of them has
    somewhere to reconcile TO:

    - `legal_concepts` is mirrored by data/concepts.json, so a deletion there
      triggers a full three-way sync and the concept is removed from the file.

    - `schema_docs` has no file. Those documents are derived from MySQL by the
      review flow, and their descriptions are often hand-curated, so there is
      nothing safe to restore them from automatically — a scan would happily
      overwrite an edited description with a fresh LLM draft.

    So a schema_docs deletion is REPORTED rather than repaired, and that is the
    whole point: deleting one silently makes its table invisible to the chatbot.
    Nothing errors, no question fails loudly — the assistant just quietly stops
    being able to answer anything about that table. This turns that into a
    warning naming the table and what to do about it.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._pending: asyncio.Event = asyncio.Event()
        self._stopping = False
        # The kind of the most recent CONCEPT deletion seen, read by the
        # debouncer when the burst settles.
        self._last_signal = ""
        # Schema-doc deletions seen since the last report, as (kind, ...). Held
        # separately because they are reported, not reconciled.
        self._schema_signals: set[str] = set()
        # Deduplicates the "cannot watch" warning so a container that is down
        # does not write one every retry.
        self._last_unavailable = ""

    async def start(self) -> None:
        if not QDRANT_WATCH_ENABLED:
            logger.info("Qdrant deletion watcher disabled (QDRANT_WATCH_ENABLED=false)")
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="qdrant-watch")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutting down
                pass
            self._task = None

    async def _run(self) -> None:
        debouncer = asyncio.create_task(self._debounce_loop(), name="qdrant-watch-debounce")
        try:
            while not self._stopping:
                started = await self._tail_once()
                if self._stopping:
                    break
                if not started:
                    # Docker missing, or the container is not running. Either is
                    # a legitimate deployment (Qdrant Cloud, a bare binary), so
                    # this is reported once and then retried quietly.
                    await asyncio.sleep(_RESTART_DELAY_SECONDS)
                else:
                    logger.info("Qdrant log stream ended; reconnecting")
                    await asyncio.sleep(1.0)
        finally:
            debouncer.cancel()

    async def _tail_once(self) -> bool:
        """Stream the container log until it ends. Returns False if it never started."""
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "logs",
                "-f",
                "--tail",
                "0",
                QDRANT_CONTAINER,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            self._report_unavailable("the docker CLI is not on PATH")
            return False
        except Exception as exc:  # noqa: BLE001
            self._report_unavailable(str(exc))
            return False

        assert process.stdout is not None
        saw_output = False

        try:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                saw_output = True
                line = raw.decode("utf-8", errors="replace").strip()
                kind, collection = classify_delete(line)
                if kind is None:
                    continue
                if collection == QDRANT_CONCEPTS_COLLECTION:
                    self._last_signal = kind
                    self._pending.set()
                elif collection == QDRANT_COLLECTION:
                    self._schema_signals.add(kind)
                    self._pending.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Qdrant log stream failed: %s", exc)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    pass

        # `docker logs` against a missing container exits immediately having
        # written its error to the stream we are reading, which counts as output.
        # The returncode is what distinguishes that from a healthy stream ending.
        if not saw_output and process.returncode not in (0, None):
            self._report_unavailable(f"`docker logs {QDRANT_CONTAINER}` exited {process.returncode}")
            return False
        return True

    def _report_unavailable(self, reason: str) -> None:
        """Warn once per reason, not once per retry."""
        if self._last_unavailable == reason:
            return
        self._last_unavailable = reason
        activity.record(
            activity.SOURCE_QDRANT_WATCH,
            "watcher_unavailable",
            (
                f"لا تتم مراقبة الحذف الخارجي من الفهرس: {reason}. "
                f"أي حذف يجري على الفهرس مباشرة لن ينعكس على النسخة الاحتياطية "
                f"حتى تُشغَّل مزامنة يدويًا."
            ),
            level=activity.LEVEL_WARNING,
            container=QDRANT_CONTAINER,
        )

    async def _debounce_loop(self) -> None:
        """Wait for a deletion signal, let the burst settle, then act on it."""
        while True:
            await self._pending.wait()
            await asyncio.sleep(QDRANT_WATCH_DEBOUNCE_SECONDS)
            self._pending.clear()

            concept_signal, self._last_signal = self._last_signal, ""
            schema_signals, self._schema_signals = self._schema_signals, set()

            if concept_signal:
                await self._reconcile_concepts(concept_signal)
            if schema_signals:
                await self._report_schema_deletion(schema_signals)

    async def _reconcile_concepts(self, signal: str) -> None:
        activity.record(
            activity.SOURCE_QDRANT_WATCH,
            signal,
            (
                "حُذفت مجموعة المفاهيم من الفهرس"
                if signal == DELETE_COLLECTION
                else "حُذفت مفاهيم من الفهرس"
            )
            + " — تجري الآن مطابقتها مع النسخة الاحتياطية.",
            level=activity.LEVEL_INFO,
            collection=QDRANT_CONCEPTS_COLLECTION,
        )

        try:
            await concept_sync.sync(trigger=f"qdrant:{signal}")
        except Exception as exc:  # noqa: BLE001 — the watcher must survive a bad sync
            logger.exception("Reconcile after a Qdrant deletion failed")
            activity.record(
                activity.SOURCE_QDRANT_WATCH,
                "reconcile_failed",
                f"فشلت المطابقة بعد الحذف من الفهرس: {exc}",
                level=activity.LEVEL_ERROR,
            )

    async def _report_schema_deletion(self, signals: set[str]) -> None:
        """Name the tables that just became unanswerable, and say so.

        Reported rather than repaired. Restoring would mean re-running the
        review flow, which writes a fresh LLM draft — and these descriptions are
        frequently edited by hand, so "restoring" one could quietly replace
        curated text with a worse machine-written version. Choosing that is the
        operator's call, so the warning tells them where the button is.

        The tables are worked out by diffing MySQL against what is left in the
        index, not read from the log: the log records that a delete happened,
        never which points it removed.
        """
        dropped_whole_collection = DELETE_COLLECTION in signals
        missing: list[str] = []

        try:
            from app.services.catalog import fetch_indexed_tables
            from app.services.introspect import introspect_schema

            live = await introspect_schema()
            indexed = await fetch_indexed_tables()
            missing = sorted(set(live) - set(indexed))
        except Exception as exc:  # noqa: BLE001 — still worth reporting without names
            logger.warning("Could not work out which schema docs were deleted: %s", exc)

        if not missing and not dropped_whole_collection:
            # A delete that removed nothing, or points for tables that no longer
            # exist in MySQL either. Nothing became unanswerable.
            logger.info("schema_docs deletion left no live table undocumented")
            return

        if missing:
            listed = ", ".join(missing)
            plural = len(missing) > 1
            message = (
                f"حُذفت أوصاف جداول من الفهرس. لم يعد النظام قادرًا على العثور على "
                f"{'هذه الجداول' if plural else 'هذا الجدول'}: {listed}. "
                f"الأسئلة عنها ستُجاب من جداول خاطئة أو لن تُجاب إطلاقًا. "
                f"اضغط 'ابحث' في تعديلات قاعدة البيانات لإعادة توثيقها."
            )
        else:
            message = (
                "حُذفت أوصاف الجداول من الفهرس بالكامل. لن يتمكّن النظام من العثور على أي "
                "جدول حتى يُعاد بناؤها — اضغط 'ابحث' في تعديلات قاعدة البيانات."
            )

        activity.record(
            activity.SOURCE_QDRANT_WATCH,
            "schema_docs_deleted",
            message,
            level=activity.LEVEL_WARNING,
            collection=QDRANT_COLLECTION,
            tables=missing,
            dropped_collection=dropped_whole_collection,
        )


watcher = QdrantWatcher()
