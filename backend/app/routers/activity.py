"""What the automated paths have been doing, for the Activity tab.

Two ways to read it: a snapshot for the initial render, and a Server-Sent Events
stream for everything after. SSE rather than polling because the events this
reports are bursty and rare — a poll would be almost always empty, and the one
time it matters (a table being documented while you watch) is exactly when you
want it immediate.

Same transport the query endpoint already uses, so it needs no new client-side
machinery.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.services import activity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/activity", tags=["activity"])

# Sent when nothing has happened for this long, to keep proxies from closing an
# idle stream. A comment line is valid SSE and is ignored by EventSource.
_KEEPALIVE_SECONDS = 20.0


@router.get("")
async def list_activity(
    limit: int = Query(100, ge=1, le=500),
    level: str | None = Query(None),
    source: str | None = Query(None),
) -> dict:
    return {
        "events": activity.recent(limit=limit, level=level, source=source),
        "warning_count": activity.warning_count(),
    }


@router.delete("")
async def clear_activity() -> dict:
    """Empty the log.

    Only the record of what happened — it does not touch the review queue, the
    schema index, or anything else. A table that was waiting for review before
    is still waiting after.
    """
    return {"cleared": activity.clear()}


@router.get("/stream")
async def stream_activity() -> StreamingResponse:
    queue = activity.subscribe()

    async def event_source() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                # `default=str` for the same reason the log file uses it: event
                # details carry arbitrary values, and an unserializable one here
                # would kill the whole stream rather than one event.
                yield f"data: {json.dumps(event.to_dict(), ensure_ascii=False, default=str)}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            activity.unsubscribe(queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Nginx buffers streamed responses by default, which holds events
            # until the buffer fills and defeats the point of streaming.
            "X-Accel-Buffering": "no",
        },
    )
