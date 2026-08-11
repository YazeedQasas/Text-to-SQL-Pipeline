"""Tests for the chat list endpoints.

The store is faked rather than pointed at MySQL: what these tests are about is
the HTTP contract — status codes, response shape, and the mapping from the three
store failures onto three different codes. Whether the SQL is right is a question
for a live database, and answering it here would make the suite need one.

The status-code mapping gets the most attention because collapsing it is the easy
mistake and each code drives different client behaviour: 404 means drop the chat
from the sidebar, 503 means retry, 500 means stop and read the logs.
"""

import pymysql
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import chat_store
from app.services.chat_store import ChatNotFound, ChatStoreError

client = TestClient(app)

CHAT_ID = "11111111-2222-3333-4444-555555555555"


def _chat(**overrides) -> dict:
    base = {
        "id": CHAT_ID,
        "title": "كم عدد القضايا المفتوحة؟",
        "created_at": 1000.0,
        "updated_at": 2000.0,
        "turn_count": 2,
    }
    base.update(overrides)
    return base


def _turn(turn_id: int = 1, **overrides) -> dict:
    base = {
        "id": turn_id,
        "question": "كم عدد القضايا المفتوحة؟",
        "sql": "SELECT COUNT(*) FROM cases",
        "answer": "هناك 42 قضية.",
        "table_names": ["cases"],
        "created_at": 1500.0,
    }
    base.update(overrides)
    return base


async def _raise(exc):
    raise exc


# --- Listing ------------------------------------------------------------------


def test_list_returns_chats(monkeypatch):
    async def fake_list(limit):
        assert limit == chat_store.DEFAULT_CHAT_LIMIT
        return [_chat()]

    monkeypatch.setattr(chat_store, "list_chats", fake_list)

    response = client.get("/api/chats")

    assert response.status_code == 200
    body = response.json()
    assert len(body["chats"]) == 1
    assert body["chats"][0]["id"] == CHAT_ID
    assert body["chats"][0]["turn_count"] == 2


def test_list_passes_through_the_limit(monkeypatch):
    seen = {}

    async def fake_list(limit):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(chat_store, "list_chats", fake_list)

    assert client.get("/api/chats?limit=7").status_code == 200
    assert seen["limit"] == 7


def test_list_rejects_an_out_of_range_limit():
    assert client.get("/api/chats?limit=0").status_code == 422
    assert client.get("/api/chats?limit=100000").status_code == 422


def test_untitled_chat_is_reported_as_null(monkeypatch):
    """A chat with no turns has no title, and must not be given a fake one."""

    async def fake_list(limit):
        return [_chat(title=None, turn_count=0)]

    monkeypatch.setattr(chat_store, "list_chats", fake_list)

    assert client.get("/api/chats").json()["chats"][0]["title"] is None


# --- Creating -----------------------------------------------------------------


def test_create_without_an_id(monkeypatch):
    async def fake_create(chat_id):
        assert chat_id is None
        return _chat(title=None, turn_count=0)

    monkeypatch.setattr(chat_store, "create_chat", fake_create)

    response = client.post("/api/chats", json={})

    assert response.status_code == 201
    assert response.json()["id"] == CHAT_ID


def test_create_adopts_a_client_generated_id(monkeypatch):
    async def fake_create(chat_id):
        assert chat_id == CHAT_ID
        return _chat(title=None, turn_count=0)

    monkeypatch.setattr(chat_store, "create_chat", fake_create)

    assert client.post("/api/chats", json={"id": CHAT_ID}).status_code == 201


def test_create_rejects_an_id_that_is_not_a_uuid_length():
    """The column is CHAR(36); a shorter id would be silently padded."""
    assert client.post("/api/chats", json={"id": "too-short"}).status_code == 422


def test_recreating_an_existing_chat_is_not_an_error(monkeypatch):
    """A retry after a dropped connection must be safe, and keep the transcript."""

    async def fake_create(chat_id):
        return _chat(turn_count=2)  # already has turns

    monkeypatch.setattr(chat_store, "create_chat", fake_create)

    response = client.post("/api/chats", json={"id": CHAT_ID})

    assert response.status_code == 201
    assert response.json()["turn_count"] == 2


# --- Reading one --------------------------------------------------------------


def test_get_returns_chat_with_transcript(monkeypatch):
    async def fake_get(chat_id):
        return _chat()

    async def fake_history(chat_id, limit=None):
        return [_turn(1), _turn(2, question="وفي غزة؟")]

    monkeypatch.setattr(chat_store, "get_chat", fake_get)
    monkeypatch.setattr(chat_store, "load_history", fake_history)

    body = client.get(f"/api/chats/{CHAT_ID}").json()

    assert body["chat"]["id"] == CHAT_ID
    assert [turn["id"] for turn in body["turns"]] == [1, 2]
    assert body["turns"][0]["sql"] == "SELECT COUNT(*) FROM cases"
    assert body["turns"][0]["table_names"] == ["cases"]


def test_get_missing_chat_is_404(monkeypatch):
    async def fake_get(chat_id):
        return None

    monkeypatch.setattr(chat_store, "get_chat", fake_get)

    assert client.get(f"/api/chats/{CHAT_ID}").status_code == 404


# --- Renaming -----------------------------------------------------------------


def test_rename(monkeypatch):
    async def fake_rename(chat_id, title):
        assert title == "قضايا غزة"
        return _chat(title=title)

    monkeypatch.setattr(chat_store, "rename_chat", fake_rename)

    response = client.patch(f"/api/chats/{CHAT_ID}", json={"title": "قضايا غزة"})

    assert response.status_code == 200
    assert response.json()["title"] == "قضايا غزة"


def test_rename_rejects_an_empty_title():
    assert client.patch(f"/api/chats/{CHAT_ID}", json={"title": ""}).status_code == 422


def test_rename_rejects_a_title_past_the_column_width():
    long_title = "x" * (chat_store.TITLE_MAX_CHARS + 1)
    assert client.patch(f"/api/chats/{CHAT_ID}", json={"title": long_title}).status_code == 422


def test_rename_missing_chat_is_404(monkeypatch):
    monkeypatch.setattr(
        chat_store, "rename_chat", lambda *a, **k: _raise(ChatNotFound(CHAT_ID))
    )

    response = client.patch(f"/api/chats/{CHAT_ID}", json={"title": "أي شيء"})

    assert response.status_code == 404


# --- Deleting -----------------------------------------------------------------


def test_delete(monkeypatch):
    async def fake_delete(chat_id):
        return True

    monkeypatch.setattr(chat_store, "delete_chat", fake_delete)

    response = client.delete(f"/api/chats/{CHAT_ID}")

    assert response.status_code == 200
    assert response.json() == {"deleted": CHAT_ID}


def test_delete_missing_chat_is_404(monkeypatch):
    async def fake_delete(chat_id):
        return False

    monkeypatch.setattr(chat_store, "delete_chat", fake_delete)

    assert client.delete(f"/api/chats/{CHAT_ID}").status_code == 404


# --- The three failures must stay three different codes -----------------------


def test_misconfiguration_is_500(monkeypatch):
    """Retrying cannot fix an .env; the client must not treat this as transient."""
    monkeypatch.setattr(
        chat_store, "list_chats", lambda *a, **k: _raise(ChatStoreError("same schema"))
    )

    assert client.get("/api/chats").status_code == 500


def test_unreachable_database_is_503(monkeypatch):
    """MySQL down IS transient, and 503 is what tells the client to retry."""
    monkeypatch.setattr(
        chat_store,
        "list_chats",
        lambda *a, **k: _raise(pymysql.OperationalError(2003, "Can't connect")),
    )

    response = client.get("/api/chats")

    assert response.status_code == 503


def test_deleted_mid_flight_is_404_not_500(monkeypatch):
    """Concurrent chats make this routine: one tab deletes what another is using."""
    monkeypatch.setattr(
        chat_store, "rename_chat", lambda *a, **k: _raise(ChatNotFound(CHAT_ID))
    )

    assert client.patch(f"/api/chats/{CHAT_ID}", json={"title": "x"}).status_code == 404


def test_an_unexpected_error_is_not_swallowed(monkeypatch):
    """Only the three known failures are mapped; anything else must propagate."""
    monkeypatch.setattr(
        chat_store, "list_chats", lambda *a, **k: _raise(RuntimeError("something else"))
    )

    with pytest.raises(RuntimeError, match="something else"):
        client.get("/api/chats")
