"""End-to-end tests for the query pipeline with all external services stubbed.

The pipeline imports `embed_text` / `search_tables` / `execute_select` by name,
so those are patched on `app.services.pipeline`; `llm` is imported as a module
and patched there.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import llm, pipeline
from app.services.retrieval import SchemaTable

client = TestClient(app)


def _table() -> SchemaTable:
    return SchemaTable(
        table_name="cases",
        description="Legal cases.",
        primary_key="case_id",
        columns=[{"name": "case_id", "type": "INT", "nullable": False, "description": "PK"}],
        foreign_keys=[],
        score=0.91,
    )


async def _chunks(*parts: str):
    for part in parts:
        yield part


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Stub every external call the pipeline makes; return the knobs tests tweak."""
    state = {"sql_parts": ["SELECT case_id ", "FROM cases"], "tables": [_table()], "rows": []}

    async def fake_embed_text(_text):
        return [0.1] * 8

    async def fake_search_tables(_vector, top_k=5):
        return state["tables"]

    async def fake_execute_select(_sql):
        return ["case_id"], state["rows"]

    monkeypatch.setattr(pipeline, "embed_text", fake_embed_text)
    monkeypatch.setattr(pipeline, "search_tables", fake_search_tables)
    monkeypatch.setattr(pipeline, "execute_select", fake_execute_select)
    monkeypatch.setattr(llm, "stream_sql", lambda *a, **k: _chunks(*state["sql_parts"]))
    monkeypatch.setattr(llm, "stream_answer", lambda *a, **k: _chunks("One ", "case ", "found."))
    return state


def _events(question: str = "how many cases?") -> list[dict]:
    with client.stream("POST", "/api/query/stream", json={"question": question}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    return [
        json.loads(frame[len("data:") :].strip())
        for frame in body.split("\n\n")
        if frame.startswith("data:")
    ]


def test_stream_reports_every_stage_in_order(stub_pipeline):
    events = _events()

    assert events[0]["type"] == "stages"
    assert [s["id"] for s in events[0]["stages"]] == [s["id"] for s in pipeline.STAGES]

    started = [e["stage"] for e in events if e["type"] == "stage" and e["status"] == "started"]
    completed = [e["stage"] for e in events if e["type"] == "stage" and e["status"] == "completed"]
    expected = [s["id"] for s in pipeline.STAGES]
    assert started == expected
    assert completed == expected


def test_stream_forwards_llm_tokens_and_final_result(stub_pipeline):
    events = _events()

    sql_tokens = [e["text"] for e in events if e["type"] == "token" and e["stage"] == "sql_generation"]
    answer_tokens = [
        e["text"] for e in events if e["type"] == "token" and e["stage"] == "answer_generation"
    ]
    assert "".join(sql_tokens) == "SELECT case_id FROM cases"
    assert "".join(answer_tokens) == "One case found."

    result = next(e["result"] for e in events if e["type"] == "result")
    assert result["answer"] == "One case found."
    assert result["sql"].startswith("SELECT case_id FROM cases")
    assert result["retrieved_tables"][0]["table_name"] == "cases"


def test_stream_reports_failure_in_band(stub_pipeline):
    stub_pipeline["tables"] = []
    events = _events()

    error = next(e for e in events if e["type"] == "error")
    assert error["status_code"] == 422
    assert "No relevant tables" in error["detail"]
    # The stage that failed announced its start but never completed.
    assert {"type": "stage", "stage": "retrieval", "status": "started", "detail": None} in events
    assert not any(
        e["type"] == "stage" and e["stage"] == "retrieval" and e["status"] == "completed"
        for e in events
    )


def test_non_streaming_endpoint_still_returns_json(stub_pipeline):
    response = client.post("/api/query", json={"question": "how many cases?"})

    assert response.status_code == 200
    assert response.json()["answer"] == "One case found."


def test_non_streaming_endpoint_maps_pipeline_error_to_http_status(stub_pipeline):
    stub_pipeline["tables"] = []

    response = client.post("/api/query", json={"question": "how many cases?"})

    assert response.status_code == 422
    assert "No relevant tables" in response.json()["detail"]
