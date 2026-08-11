"""Tests for running a question inside a stored conversation.

The pipeline itself is stubbed. What matters here is the layer around it: that
history comes from the right chat, that the turn is saved, that `saved` tells the
truth, and that a failed save never costs the user a good answer.

The isolation test is the point of the whole feature — two chats must not be able
to see each other's turns — so it asserts on what the pipeline was actually
handed rather than on the response.
"""

import asyncio

import pytest

from app.models import ContextUsage, HistoryTurn, QueryRequest, QueryResponse, RetrievedTable
from app.services import chat_store, pipeline
from app.services.chat_store import ChatNotFound
from app.services.pipeline import StageEvent, run_chat_turn

CHAT_A = "aaaaaaaa-1111-2222-3333-444444444444"
CHAT_B = "bbbbbbbb-1111-2222-3333-444444444444"


def _stored_turn(question: str, tables=None) -> dict:
    return {
        "id": 1,
        "question": question,
        "sql": "SELECT 1",
        "answer": "جواب",
        "table_names": tables if tables is not None else ["cases"],
        "created_at": 0.0,
    }


def _response(answer: str = "هناك 42 قضية.") -> QueryResponse:
    return QueryResponse(
        answer=answer,
        sql="SELECT COUNT(*) FROM cases",
        columns=["n"],
        rows=[{"n": 42}],
        retrieved_tables=[
            RetrievedTable(table_name="cases", description="القضايا", score=0.9)
        ],
        usage=ContextUsage(used_tokens=100, limit_tokens=15200, history_turns=0),
    )


@pytest.fixture
def fake_store(monkeypatch):
    """A store that records what it was asked, keyed by chat."""
    state = {"histories": {}, "appended": [], "fail_with": None}

    async def load_history(chat_id, limit=None):
        return state["histories"].get(chat_id, [])

    async def append_turn(chat_id, question, sql, answer, table_names):
        if state["fail_with"] is not None:
            raise state["fail_with"]
        state["appended"].append(
            {
                "chat_id": chat_id,
                "question": question,
                "sql": sql,
                "answer": answer,
                "table_names": table_names,
            }
        )
        return {"id": len(state["appended"])}

    monkeypatch.setattr(chat_store, "load_history", load_history)
    monkeypatch.setattr(chat_store, "append_turn", append_turn)
    return state


@pytest.fixture
def fake_pipeline(monkeypatch):
    """A pipeline that records the history it was handed and yields one result."""
    seen = {"history": None, "question": None}

    async def run_pipeline(question, history=None):
        seen["question"] = question
        seen["history"] = list(history or [])
        yield StageEvent("embedding", "started")
        yield _response()

    monkeypatch.setattr(pipeline, "run_pipeline", run_pipeline)
    return seen


async def _drain(chat_id, question):
    return [event async for event in run_chat_turn(chat_id, question)]


# --- Each chat sees only its own turns ----------------------------------------


def test_history_comes_from_the_named_chat(fake_store, fake_pipeline):
    fake_store["histories"][CHAT_A] = [_stored_turn("سؤال من المحادثة الأولى")]
    fake_store["histories"][CHAT_B] = [_stored_turn("سؤال من المحادثة الثانية")]

    asyncio.run(_drain(CHAT_A, "متابعة"))

    assert [turn.question for turn in fake_pipeline["history"]] == ["سؤال من المحادثة الأولى"]


def test_a_chats_turns_never_leak_into_another(fake_store, fake_pipeline):
    """The whole feature in one assertion."""
    fake_store["histories"][CHAT_A] = [_stored_turn("أ"), _stored_turn("أأ")]
    fake_store["histories"][CHAT_B] = [_stored_turn("ب")]

    asyncio.run(_drain(CHAT_B, "متابعة"))

    questions = [turn.question for turn in fake_pipeline["history"]]
    assert questions == ["ب"]
    assert "أ" not in questions


def test_an_empty_chat_starts_with_no_history(fake_store, fake_pipeline):
    asyncio.run(_drain(CHAT_A, "أول سؤال"))

    assert fake_pipeline["history"] == []


def test_stored_turns_become_history_turns(fake_store, fake_pipeline):
    """The store's dicts must map cleanly onto what the prompt builder expects."""
    fake_store["histories"][CHAT_A] = [_stored_turn("سؤال", tables=["cases", "courts"])]

    asyncio.run(_drain(CHAT_A, "متابعة"))

    turn = fake_pipeline["history"][0]
    assert isinstance(turn, HistoryTurn)
    assert turn.sql == "SELECT 1"
    assert turn.table_names == ["cases", "courts"]


# --- Saving -------------------------------------------------------------------


def test_the_turn_is_appended_to_the_chat(fake_store, fake_pipeline):
    asyncio.run(_drain(CHAT_A, "كم عدد القضايا؟"))

    assert len(fake_store["appended"]) == 1
    saved = fake_store["appended"][0]
    assert saved["chat_id"] == CHAT_A
    assert saved["question"] == "كم عدد القضايا؟"
    assert saved["sql"] == "SELECT COUNT(*) FROM cases"
    assert saved["table_names"] == ["cases"]


def test_response_reports_saved(fake_store, fake_pipeline):
    events = asyncio.run(_drain(CHAT_A, "سؤال"))
    result = [e for e in events if isinstance(e, QueryResponse)][0]

    assert result.saved is True


def test_only_the_final_result_is_saved(fake_store, fake_pipeline):
    """Stage events are progress, not turns."""
    asyncio.run(_drain(CHAT_A, "سؤال"))

    assert len(fake_store["appended"]) == 1


def test_stage_events_still_reach_the_client(fake_store, fake_pipeline):
    events = asyncio.run(_drain(CHAT_A, "سؤال"))

    assert any(isinstance(e, StageEvent) for e in events)


# --- A failed save must not cost the answer -----------------------------------


def test_deleted_chat_still_returns_the_answer(fake_store, fake_pipeline):
    """Delete a chat mid-stream and the answer still arrives, marked unsaved."""
    fake_store["fail_with"] = ChatNotFound(CHAT_A)

    events = asyncio.run(_drain(CHAT_A, "سؤال"))
    result = [e for e in events if isinstance(e, QueryResponse)][0]

    assert result.answer == "هناك 42 قضية."
    assert result.saved is False


def test_database_failure_still_returns_the_answer(fake_store, fake_pipeline):
    import pymysql

    fake_store["fail_with"] = pymysql.OperationalError(2006, "server has gone away")

    events = asyncio.run(_drain(CHAT_A, "سؤال"))
    result = [e for e in events if isinstance(e, QueryResponse)][0]

    assert result.answer == "هناك 42 قضية."
    assert result.saved is False


# --- The request contract -----------------------------------------------------


def test_chat_id_alone_is_valid():
    request = QueryRequest(question="سؤال", chat_id=CHAT_A)

    assert request.chat_id == CHAT_A
    assert request.history == []


def test_history_alone_is_still_valid():
    """The stateless path has to keep working — scripted callers use it."""
    request = QueryRequest(
        question="سؤال", history=[HistoryTurn(question="سابق", sql="", answer="")]
    )

    assert request.chat_id is None
    assert len(request.history) == 1


def test_neither_is_valid():
    """A first question in a fresh conversation has no history at all."""
    assert QueryRequest(question="سؤال").history == []


def test_both_together_are_rejected():
    """Silently preferring one would let a client believe in a history the model
    never saw."""
    with pytest.raises(ValueError, match="not both"):
        QueryRequest(
            question="سؤال",
            chat_id=CHAT_A,
            history=[HistoryTurn(question="سابق", sql="", answer="")],
        )


def test_chat_id_must_be_uuid_length():
    with pytest.raises(ValueError):
        QueryRequest(question="سؤال", chat_id="short")
