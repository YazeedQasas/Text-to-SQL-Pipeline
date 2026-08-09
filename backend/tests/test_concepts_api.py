"""The admin page's concept endpoints: list what Qdrant has, write one back.

These cover the inversion the admin page depends on. Everywhere else in this
codebase `concepts.json` is the authored side and Qdrant follows it; here the
person is typing into the live collection, and the file is a backup written
afterwards. The thing that must not regress is that a save reaches Qdrant even
when the file cannot be written, and that the snapshot moves with it — a stale
snapshot would make the next three-way sync report the edit as a conflict, or
undo it.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.config import EMBEDDING_DIM
from app.main import app
from app.services import activity, concept_store, concept_sync, retrieval
from app.services.concept_docs import ConceptDoc

client = TestClient(app)


def _concept(concept_id: str, term: str = "القضية المختنقة") -> ConceptDoc:
    return ConceptDoc(
        id=concept_id,
        term=term,
        definition="الدعوى التي تجاوزت المدة المقررة.",
        sql="case_congestion.congestion_status = 'مختنقة'",
        tables=["case_congestion"],
        aliases=["مختنقة"],
    )


@pytest.fixture
def stub_concepts(monkeypatch, tmp_path):
    """Point the file and snapshot at a tmp dir, and stub Qdrant and embeddings."""
    state = {
        "qdrant": {},
        "upserted": [],  # list[(ConceptDoc, vector)]
        "cache_refreshed": False,
    }

    concepts_file = tmp_path / "concepts.json"
    snapshot_file = tmp_path / ".concepts-sync-state.json"

    async def fake_fetch_concepts():
        return dict(state["qdrant"])

    async def fake_ensure_collection():
        return None

    async def fake_upsert_concepts(concepts, vectors):
        for concept, vector in zip(concepts, vectors):
            state["upserted"].append((concept, vector))
            state["qdrant"][concept.id] = concept

    async def fake_embed_texts(texts):
        return [[0.1] * EMBEDDING_DIM for _ in texts]

    async def fake_load_all_concepts(force=False):
        state["cache_refreshed"] = True
        return []

    monkeypatch.setattr(concept_store, "fetch_concepts", fake_fetch_concepts)
    monkeypatch.setattr(concept_store, "ensure_collection", fake_ensure_collection)
    monkeypatch.setattr(concept_store, "upsert_concepts", fake_upsert_concepts)
    monkeypatch.setattr(concept_sync, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(retrieval, "load_all_concepts", fake_load_all_concepts)
    monkeypatch.setattr(concept_sync, "CONCEPTS_FILE", str(concepts_file))
    monkeypatch.setattr(concept_sync, "CONCEPTS_SYNC_STATE_FILE", str(snapshot_file))
    # A save records an activity event, and the log is a real file under data/.
    # Same isolation test_activity.py uses.
    monkeypatch.setattr(activity, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")

    from app.routers import concepts as concepts_router

    monkeypatch.setattr(concepts_router, "CONCEPTS_FILE", str(concepts_file))

    state["concepts_file"] = concepts_file
    state["snapshot_file"] = snapshot_file
    return state


# --- Listing ------------------------------------------------------------------


def test_live_listing_comes_from_qdrant_not_the_file(stub_concepts):
    """The file could hold a definition that was never synced; the list must not."""
    stub_concepts["qdrant"] = {"congested": _concept("congested")}
    stub_concepts["concepts_file"].write_text(
        json.dumps({"concepts": [_concept("only_in_file").to_dict()]}, ensure_ascii=False),
        encoding="utf-8",
    )

    body = client.get("/api/concepts/live").json()

    assert [c["id"] for c in body["concepts"]] == ["congested"]
    assert body["qdrant_count"] == 1
    assert body["in_sync"] is False


def test_live_listing_follows_the_file_order_then_appends_the_rest(stub_concepts):
    """Stable order: a list that reshuffles on every poll cannot be edited."""
    stub_concepts["qdrant"] = {
        "third": _concept("third"),
        "first": _concept("first"),
        "second": _concept("second"),
    }
    stub_concepts["concepts_file"].write_text(
        json.dumps(
            {"concepts": [_concept("first").to_dict(), _concept("second").to_dict()]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    body = client.get("/api/concepts/live").json()

    assert [c["id"] for c in body["concepts"]] == ["first", "second", "third"]


def test_live_listing_survives_an_unreadable_backup_file(stub_concepts):
    """The file is a backup. A corrupt one must not hide the live glossary."""
    stub_concepts["qdrant"] = {"congested": _concept("congested")}
    stub_concepts["concepts_file"].write_text("{ not json", encoding="utf-8")

    response = client.get("/api/concepts/live")

    assert response.status_code == 200
    assert [c["id"] for c in response.json()["concepts"]] == ["congested"]


# --- Saving -------------------------------------------------------------------


def _payload(**overrides) -> dict:
    return {**_concept("congested").to_dict(), **overrides}


def test_saving_writes_to_qdrant_and_mirrors_the_file_and_snapshot(stub_concepts):
    response = client.put("/api/concepts/entry", json=_payload())

    assert response.status_code == 200
    saved, vector = stub_concepts["upserted"][0]
    assert saved.id == "congested"
    assert len(vector) == EMBEDDING_DIM

    on_disk = json.loads(stub_concepts["concepts_file"].read_text(encoding="utf-8"))
    assert [c["id"] for c in on_disk["concepts"]] == ["congested"]

    # Without this the next three-way sync would see a concept that changed on
    # both sides since the last agreement and report a conflict over it.
    snapshot = json.loads(stub_concepts["snapshot_file"].read_text(encoding="utf-8"))
    assert [c["id"] for c in snapshot["concepts"]] == ["congested"]


def test_saving_an_existing_concept_replaces_it_rather_than_appending(stub_concepts):
    stub_concepts["concepts_file"].write_text(
        json.dumps(
            {"concepts": [_concept("other").to_dict(), _concept("congested").to_dict()]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    client.put("/api/concepts/entry", json=_payload(definition="تعريف جديد"))

    on_disk = json.loads(stub_concepts["concepts_file"].read_text(encoding="utf-8"))
    assert [c["id"] for c in on_disk["concepts"]] == ["other", "congested"]
    assert on_disk["concepts"][1]["definition"] == "تعريف جديد"


def test_saving_refreshes_the_query_cache(stub_concepts):
    """Otherwise the edit does not reach an answer until the TTL expires."""
    client.put("/api/concepts/entry", json=_payload())

    assert stub_concepts["cache_refreshed"] is True


def test_a_write_that_reached_qdrant_is_not_undone_by_a_broken_backup(stub_concepts):
    """The file is secondary. A save that landed where it counts must report success."""
    stub_concepts["concepts_file"].write_text("{ not json", encoding="utf-8")

    response = client.put("/api/concepts/entry", json=_payload())

    assert response.status_code == 200
    assert [c.id for c, _ in stub_concepts["upserted"]] == ["congested"]


@pytest.mark.parametrize("field", ["term", "definition"])
def test_a_concept_with_nothing_to_embed_is_refused(stub_concepts, field):
    """Term and definition are what gets embedded — without them it can never match."""
    response = client.put("/api/concepts/entry", json=_payload(**{field: "   "}))

    assert response.status_code == 422
    assert stub_concepts["upserted"] == []
