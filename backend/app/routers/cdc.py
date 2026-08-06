"""The endpoint Debezium Server posts change events to.

Deliberately does almost nothing. The sink is a dumb HTTP client with a retry
policy: if this handler is slow it stalls the connector, and if it returns a 5xx
the same event is redelivered. So the request is parsed, buffered, and
acknowledged — the documenting happens on a background worker, which is also
what lets several events about the same table be coalesced into one review.

Events are accepted even when the body is not something we recognise. Debezium
sends heartbeats and transaction markers down the same sink, and answering those
with an error would make the connector retry them forever.
"""

import logging

from fastapi import APIRouter, Request

from app.models import CdcStatusResponse
from app.services import cdc, review_queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cdc", tags=["cdc"])


@router.post("/events")
async def receive_events(request: Request) -> dict:
    """Accept one change event, or a batch of them, from the Debezium HTTP sink."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — a body we cannot parse is not worth a retry
        logger.warning("CDC event with an unparseable body was discarded")
        return {"accepted": 0}

    accepted = cdc.service.ingest(body)
    return {"accepted": accepted}


@router.get("/status", response_model=CdcStatusResponse)
async def status() -> CdcStatusResponse:
    """What the CDC path is doing, for the Activity tab's header."""
    return CdcStatusResponse(
        **cdc.service.status(),
        awaiting_review=review_queue.count(indexed=False),
        auto_documented=review_queue.count(indexed=True),
    )
