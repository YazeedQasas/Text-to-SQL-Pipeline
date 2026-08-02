import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.models import QueryRequest, QueryResponse
from app.services.pipeline import (
    STAGES,
    PipelineError,
    StageEvent,
    TokenEvent,
    run_pipeline,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["query"])


@router.post("/query", response_model=QueryResponse)
async def run_query(request: QueryRequest) -> QueryResponse:
    """Run the pipeline and return only the final result.

    Stage and token events are discarded; use /api/query/stream to observe
    progress while the pipeline runs.
    """
    try:
        async for event in run_pipeline(request.question):
            if isinstance(event, QueryResponse):
                return event
    except PipelineError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    raise HTTPException(status_code=500, detail="Pipeline finished without producing a result.")


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@router.post("/query/stream")
async def run_query_stream(request: QueryRequest) -> StreamingResponse:
    """Run the pipeline, reporting progress as server-sent events.

    Event payloads (one JSON object per `data:` frame):
      {"type": "stages", "stages": [{"id", "label"}, ...]}   once, up-front
      {"type": "stage",  "stage", "status", "detail", "content"}  per stage boundary
                                       ("content" is the full text behind
                                        "detail", or null — see StageEvent)
      {"type": "token",  "stage", "text"}                    partial LLM output
      {"type": "result", "result": <QueryResponse>}          on success
      {"type": "error",  "detail", "status_code"}            on failure

    Errors are reported in-band rather than as an HTTP status, because the
    response headers are already sent by the time the pipeline can fail.
    """

    async def event_stream() -> AsyncIterator[str]:
        yield _sse({"type": "stages", "stages": STAGES})
        try:
            async for event in run_pipeline(request.question):
                if isinstance(event, StageEvent):
                    yield _sse(
                        {
                            "type": "stage",
                            "stage": event.stage,
                            "status": event.status,
                            "detail": event.detail,
                            "content": event.content,
                        }
                    )
                elif isinstance(event, TokenEvent):
                    yield _sse({"type": "token", "stage": event.stage, "text": event.text})
                else:
                    yield _sse({"type": "result", "result": event.model_dump(mode="json")})
        except PipelineError as exc:
            yield _sse({"type": "error", "detail": exc.detail, "status_code": exc.status_code})
        except Exception as exc:  # noqa: BLE001 — must surface in-band, the stream is already open
            logger.exception("Unexpected pipeline failure")
            yield _sse({"type": "error", "detail": f"Unexpected server error: {exc}", "status_code": 500})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # stop nginx-style proxies from buffering the stream
        },
    )
