import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import CORS_ALLOW_ORIGINS
from app.routers.query import router as query_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Text-to-SQL Retrieval System", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(query_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
