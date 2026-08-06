"""The activity log behind the Activity tab.

Everything that happens without a human asking for it — a table documented off a
CDC event, a concept synced after someone edited the JSON, a reconcile refused
because it wanted to delete too much — is recorded here and streamed to the
browser.

This exists because automation removed the person who used to be watching. The
manual catalog flow shows its work in the response to the button that triggered
it; a CDC event fires at 3am with nobody there, so the record has to outlive the
request that produced it.

Storage is an append-only JSONL file plus an in-memory ring of the most recent
events. Not a database: this is a log, it is read newest-first, it is never
queried by anything but the tab, and adding a database would make it the only
stateful dependency the backend has.

Writes are small synchronous appends rather than threaded IO. An event is a few
hundred bytes and they arrive at human speed — a thread pool would cost more in
complexity than the blocking write costs in latency.
"""

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.config import ACTIVITY_LOG_FILE, ACTIVITY_RING_SIZE

logger = logging.getLogger(__name__)

LEVEL_INFO = "info"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"

SOURCE_CDC = "cdc"
SOURCE_CONCEPTS = "concepts"
SOURCE_QDRANT_WATCH = "qdrant_watch"
SOURCE_CATALOG = "catalog"

# Bound on what one subscriber may fall behind by before it is dropped. A tab
# left open on a sleeping laptop must not be able to pin every event since it
# slept in memory.
_SUBSCRIBER_QUEUE_SIZE = 200


@dataclass
class ActivityEvent:
    id: str
    ts: float
    source: str
    level: str
    event: str
    message: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


_ring: deque[ActivityEvent] = deque(maxlen=ACTIVITY_RING_SIZE)
_subscribers: set[asyncio.Queue] = set()
_loaded = False


def _log_path() -> Path:
    return Path(ACTIVITY_LOG_FILE)


def _append_to_file(event: ActivityEvent) -> None:
    """Persist one event. Never raises — losing the log must not fail the work.

    A CDC-triggered documentation run that succeeded but could not write its log
    line has still succeeded, and turning that into an exception would roll back
    real work over a bookkeeping problem.
    """
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            # `default=str` because details carry whatever the caller passed —
            # including sample-row values, which can be Decimal or date.
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 — logging must not break the caller
        logger.warning("Could not append to the activity log: %s", exc)


def load_recent_from_disk() -> None:
    """Warm the ring from the tail of the log file, once, at startup.

    Without this a backend restart shows an empty Activity tab even though the
    history is right there on disk — and the first thing anyone does after
    something goes wrong is restart the backend and then look at the log.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True

    path = _log_path()
    if not path.exists():
        return

    try:
        # The whole file is read to take its tail. It is bounded in practice
        # (events arrive at human speed) and this runs exactly once per process;
        # a reverse-seek reader would be more code than the case justifies.
        with path.open("r", encoding="utf-8") as handle:
            tail = deque(handle, maxlen=ACTIVITY_RING_SIZE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read the activity log at %s: %s", path, exc)
        return

    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            _ring.append(ActivityEvent(**payload))
        except Exception:  # noqa: BLE001 — one bad line must not lose the rest
            continue


def record(
    source: str,
    event: str,
    message: str,
    level: str = LEVEL_INFO,
    **details: object,
) -> ActivityEvent:
    """Record one event: to disk, to the ring, and to every open Activity tab.

    Synchronous on purpose, so it can be called from anywhere — including the
    non-async paths in the sync engine — without threading an await through code
    that has nothing else to await.
    """
    entry = ActivityEvent(
        id=uuid.uuid4().hex,
        ts=time.time(),
        source=source,
        level=level,
        event=event,
        message=message,
        details=dict(details),
    )

    _ring.append(entry)
    _append_to_file(entry)
    _publish(entry)

    log_at = {LEVEL_ERROR: logging.ERROR, LEVEL_WARNING: logging.WARNING}.get(level, logging.INFO)
    logger.log(log_at, "[%s/%s] %s", source, event, message)

    return entry


def _publish(entry: ActivityEvent) -> None:
    """Fan the event out to live SSE subscribers, dropping any that fell behind."""
    for queue in list(_subscribers):
        try:
            queue.put_nowait(entry)
        except asyncio.QueueFull:
            # A subscriber this far behind has lost the thread of the stream
            # anyway; it reconnects and re-reads from `recent`.
            _subscribers.discard(queue)


def subscribe() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)


def recent(limit: int = 100, level: str | None = None, source: str | None = None) -> list[dict]:
    """Most recent events first, optionally filtered by level or source."""
    events = list(_ring)
    if level:
        events = [event for event in events if event.level == level]
    if source:
        events = [event for event in events if event.source == source]
    events.reverse()
    return [event.to_dict() for event in events[:limit]]


def clear() -> int:
    """Empty the log, on disk and in memory. Returns how many events went.

    Leaves one event behind saying it was cleared. An empty log and a log
    somebody emptied look identical otherwise, and the difference matters when
    you are trying to work out why there is no record of something.

    Live subscribers are not disconnected — they simply stop seeing history they
    already have on screen, and the "cleared" event tells them why.
    """
    removed = len(_ring)
    _ring.clear()

    path = _log_path()
    try:
        if path.exists():
            # Truncate rather than delete, so the file keeps whatever
            # permissions and ownership it was created with.
            path.write_text("", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — clearing memory still succeeded
        logger.warning("Could not truncate the activity log at %s: %s", path, exc)

    record(
        SOURCE_CATALOG,
        "log_cleared",
        f"Activity log cleared — {removed} event{'' if removed == 1 else 's'} removed.",
        removed=removed,
    )
    return removed


def warning_count() -> int:
    """Unresolved-looking events, for the badge on the tab."""
    return sum(1 for event in _ring if event.level in (LEVEL_WARNING, LEVEL_ERROR))


def reset_for_tests() -> None:
    global _loaded
    _ring.clear()
    _subscribers.clear()
    _loaded = False
