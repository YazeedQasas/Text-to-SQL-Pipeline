import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import CORS_ALLOW_ORIGINS
from app.routers.activity import router as activity_router
from app.routers.catalog import router as catalog_router
from app.routers.cdc import router as cdc_router
from app.routers.concepts import router as concepts_router
from app.routers.query import router as query_router
from app.services import activity, cdc, qdrant_watch

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop the two background watchers.

    Both are best-effort by design. A backend whose Qdrant log watcher cannot
    attach, or whose CDC worker is switched off, still answers questions and
    still runs the manual catalog flow — the automation is an addition to those
    paths, not a dependency of them. So a failure to start is recorded in the
    activity log and moved past, rather than raised, which would take the whole
    API down over a background task.
    """
    activity.load_recent_from_disk()

    try:
        await cdc.service.start()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not start the CDC worker")
        activity.record(
            activity.SOURCE_CDC,
            "worker_start_failed",
            f"لم يبدأ مراقب التغييرات: {exc}. لن يتم توثيق الجداول الجديدة تلقائيًا "
            f"حتى إعادة تشغيل النظام.",
            level=activity.LEVEL_ERROR,
        )

    try:
        await qdrant_watch.watcher.start()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not start the Qdrant deletion watcher")
        activity.record(
            activity.SOURCE_QDRANT_WATCH,
            "watcher_start_failed",
            f"لم يبدأ مراقب الحذف في الفهرس: {exc}.",
            level=activity.LEVEL_ERROR,
        )

    yield

    await qdrant_watch.watcher.stop()
    await cdc.service.stop()


app = FastAPI(title="Text-to-SQL Retrieval System", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(query_router)
app.include_router(catalog_router)
app.include_router(cdc_router)
app.include_router(concepts_router)
app.include_router(activity_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
