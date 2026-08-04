"""Tests for schema retrieval and the conversation carry-over.

The Qdrant client is stubbed at the call boundary rather than mocked away, so
the filter objects are still really constructed — a filter that fails Pydantic
validation (as `min_should=1` did against qdrant-client 1.9) shows up here
instead of on the first follow-up question.

No pytest-asyncio in this project, so the async functions are driven with
asyncio.run().
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.services import retrieval


def _payload(name: str) -> dict:
    return {
        "table_name": name,
        "description": f"جدول {name}.",
        "primary_key": f"{name}_id",
        "columns": [],
        "foreign_keys": [],
    }


@pytest.fixture
def stub_qdrant(monkeypatch):
    """Capture what the module asks Qdrant for, and answer with fixed payloads."""
    calls = {"scroll_filter": None, "search_limit": None}

    async def fake_scroll(collection_name, scroll_filter, limit, **kwargs):
        calls["scroll_filter"] = scroll_filter
        names = _names_in(scroll_filter)
        return [SimpleNamespace(payload=_payload(name)) for name in names], None

    async def fake_search(collection_name, query_vector, limit, **kwargs):
        calls["search_limit"] = limit
        return [SimpleNamespace(payload=_payload("cases"), score=0.9)]

    monkeypatch.setattr(retrieval._client, "scroll", fake_scroll)
    monkeypatch.setattr(retrieval._client, "search", fake_search)
    return calls


def _names_in(scroll_filter) -> list[str]:
    """Pull the requested table names back out of the constructed filter."""
    for condition in scroll_filter.must:
        match = getattr(condition, "match", None)
        if condition.key == "table_name" and match is not None:
            return list(match.any)
    return []


def test_fetch_by_name_builds_a_filter_the_client_accepts(stub_qdrant):
    tables = asyncio.run(retrieval.fetch_tables_by_name(["cases", "judges"]))

    # Constructing the filter at all is the regression under test.
    assert _names_in(stub_qdrant["scroll_filter"]) == ["cases", "judges"]
    assert [table.table_name for table in tables] == ["cases", "judges"]
    # Carried tables must never outrank a table the new question matched.
    assert all(table.score == 0.0 for table in tables)


def test_fetch_by_name_skips_qdrant_entirely_when_asked_for_nothing(stub_qdrant):
    assert asyncio.run(retrieval.fetch_tables_by_name([])) == []
    assert stub_qdrant["scroll_filter"] is None


def test_carryover_appends_only_tables_not_already_retrieved(stub_qdrant):
    # Search returns "cases"; asking to carry it plus "judges" must fetch judges only.
    tables = asyncio.run(retrieval.search_with_carryover([0.1] * 8, ["cases", "judges"]))

    assert [table.table_name for table in tables] == ["cases", "judges"]
    assert _names_in(stub_qdrant["scroll_filter"]) == ["judges"]


def test_carryover_is_capped(stub_qdrant, monkeypatch):
    monkeypatch.setattr(retrieval, "CONTEXT_CARRY_TABLES", 2)

    tables = asyncio.run(
        retrieval.search_with_carryover([0.1] * 8, ["a", "b", "c", "d", "e"])
    )

    assert [table.table_name for table in tables] == ["cases", "a", "b"]


def test_no_history_means_no_extra_qdrant_call(stub_qdrant):
    tables = asyncio.run(retrieval.search_with_carryover([0.1] * 8, []))

    assert [table.table_name for table in tables] == ["cases"]
    assert stub_qdrant["scroll_filter"] is None


# --- Domain concepts ---------------------------------------------------------


def _concept_payload(term: str, tables: list[str]) -> dict:
    return {
        "term": term,
        "aliases": [f"{term}-alias"],
        "definition": f"تعريف {term}.",
        "sql": f"{tables[0]}.col = '{term}'",
        "tables": tables,
    }


@pytest.fixture
def stub_two_collections(monkeypatch):
    """Stub both collections, so concept search and schema search are separable."""
    state = {
        "schema_hits": ["cases", "hearings", "judges", "courts", "parties"],
        # Payloads in the glossary collection; `gram_scores` maps a term to the
        # score its best question-fragment achieved.
        "concepts": [],
        "gram_scores": {},
        "exists": True,
        "search_limit": None,
        "scrolled": [],
    }

    async def fake_collection_exists(collection_name):
        return state["exists"]

    async def fake_search(collection_name, query_vector, limit, **kwargs):
        state["search_limit"] = limit
        return [
            SimpleNamespace(payload=_payload(name), score=0.9 - index / 100)
            for index, name in enumerate(state["schema_hits"])
        ]

    async def fake_search_batch(collection_name, requests, **kwargs):
        # Every fragment returns the same best-scoring concept; the module is
        # responsible for taking the max and applying the threshold.
        hits = [
            SimpleNamespace(payload=p, score=state["gram_scores"].get(p["term"], 0.0))
            for p in state["concepts"]
        ]
        hits.sort(key=lambda h: -h.score)
        return [hits[:1] for _ in requests]

    async def fake_scroll(collection_name, **kwargs):
        if collection_name == retrieval.QDRANT_CONCEPTS_COLLECTION:
            return [SimpleNamespace(payload=p) for p in state["concepts"]], None
        names = _names_in(kwargs["scroll_filter"])
        state["scrolled"].append(names)
        return [SimpleNamespace(payload=_payload(name)) for name in names], None

    monkeypatch.setattr(retrieval._client, "collection_exists", fake_collection_exists)
    monkeypatch.setattr(retrieval._client, "search", fake_search)
    monkeypatch.setattr(retrieval._client, "search_batch", fake_search_batch)
    monkeypatch.setattr(retrieval._client, "scroll", fake_scroll)
    # The glossary is cached across requests; every test starts from a clean one.
    monkeypatch.setattr(retrieval, "_concept_cache", None)
    monkeypatch.setattr(retrieval, "_concept_cache_loaded_at", 0.0)
    return state


def test_missing_concepts_collection_is_not_an_error(stub_two_collections):
    """The glossary is optional: a deployment that never ingested it still answers."""
    stub_two_collections["exists"] = False

    assert asyncio.run(retrieval.search_concepts("أي سؤال", [])) == []


def test_a_term_written_in_the_question_is_matched_without_any_vector(stub_two_collections):
    """The lexical signal: free, exact, and unaffected by question length."""
    stub_two_collections["concepts"] = [_concept_payload("القضايا المدورة", ["cases"])]

    # No gram vectors at all — the vector signal cannot contribute here.
    found = asyncio.run(
        retrieval.search_concepts("أي محكمة لديها أكبر عدد من القضايا المدورة؟", [])
    )

    assert [c.term for c in found] == ["القضايا المدورة"]
    assert found[0].matched_by == "lexical"


def test_lexical_match_survives_spelling_and_the_definite_article(stub_two_collections):
    """Normalization is what lets a question's spelling reach the stored term."""
    stub_two_collections["concepts"] = [_concept_payload("المستدعى ضده", ["case_parties"])]

    found = asyncio.run(retrieval.search_concepts("من يمثل مستدعي ضده في هذه؟", []))

    assert [c.term for c in found] == ["المستدعى ضده"]


def test_vector_signal_catches_a_term_the_question_does_not_spell_out(stub_two_collections):
    """The morphology case lexical misses — e.g. a plural of a singular term."""
    stub_two_collections["concepts"] = [_concept_payload("التشريع الساري", ["legislations"])]
    stub_two_collections["gram_scores"] = {"التشريع الساري": 0.61}

    found = asyncio.run(
        retrieval.search_concepts("ما التشريعات السارية؟", [("التشريعات السارية", [0.1] * 8)])
    )

    assert [c.term for c in found] == ["التشريع الساري"]
    assert found[0].matched_by == "vector"


def test_a_fragment_below_the_threshold_is_abstained_on(stub_two_collections):
    """Only an absolute threshold can say "nothing here is relevant"."""
    stub_two_collections["concepts"] = [_concept_payload("القضايا البسيطة", ["cases"])]
    stub_two_collections["gram_scores"] = {"القضايا البسيطة": 0.51}

    found = asyncio.run(
        retrieval.search_concepts("ما هي عناوين القضايا المدنية؟", [("عناوين القضايا", [0.1] * 8)])
    )

    assert found == []


def test_lexical_hits_lead_so_they_claim_the_boost_slots_first(stub_two_collections):
    stub_two_collections["concepts"] = [
        _concept_payload("القضايا المدورة", ["cases"]),
        _concept_payload("التشريع الساري", ["legislations"]),
    ]
    stub_two_collections["gram_scores"] = {"التشريع الساري": 0.99}

    found = asyncio.run(
        retrieval.search_concepts("القضايا المدورة والتشريعات", [("التشريعات", [0.1] * 8)])
    )

    # The vector hit scores higher, but a term written outright still leads.
    assert [c.matched_by for c in found] == ["lexical", "vector"]
    assert found[0].term == "القضايا المدورة"


def test_concepts_promote_their_tables_without_growing_the_prompt(stub_two_collections):
    """The whole point: steer WHICH tables, never HOW MANY."""
    concepts = [retrieval.Concept("المختنقة", [], "تعريف", "sql", ["courts"], 0.7)]

    tables = asyncio.run(retrieval.search_with_carryover([0.1] * 8, [], concepts))

    assert [t.table_name for t in tables][0] == "courts"
    assert len(tables) == retrieval.RETRIEVAL_TOP_K


def test_a_concept_table_missing_from_search_is_fetched_by_name(stub_two_collections):
    """The case this feature exists for — the question's embedding missed the table."""
    concepts = [retrieval.Concept("المختنقة", [], "تعريف", "sql", ["case_congestion"], 0.7)]

    tables = asyncio.run(retrieval.search_with_carryover([0.1] * 8, [], concepts))

    assert stub_two_collections["scrolled"] == [["case_congestion"]]
    assert [t.table_name for t in tables][0] == "case_congestion"
    assert len(tables) == retrieval.RETRIEVAL_TOP_K


def test_concept_tables_are_capped_so_vector_hits_keep_slots(stub_two_collections, monkeypatch):
    monkeypatch.setattr(retrieval, "CONCEPT_BOOST_TABLES", 2)
    concepts = [
        retrieval.Concept("أ", [], "تعريف", "sql", ["courts", "judges", "parties"], 0.7)
    ]

    tables = asyncio.run(retrieval.search_with_carryover([0.1] * 8, [], concepts))

    assert [t.table_name for t in tables][:2] == ["courts", "judges"]
    assert len(tables) == retrieval.RETRIEVAL_TOP_K


def test_no_concepts_leaves_schema_retrieval_exactly_as_it_was(stub_two_collections):
    """No matched concept must not widen the search or reorder anything."""
    tables = asyncio.run(retrieval.search_with_carryover([0.1] * 8, []))

    assert stub_two_collections["search_limit"] == retrieval.RETRIEVAL_TOP_K
    assert [t.table_name for t in tables] == stub_two_collections["schema_hits"]


def test_concepts_widen_the_search_so_promotion_has_candidates(stub_two_collections):
    concepts = [retrieval.Concept("أ", [], "تعريف", "sql", ["courts"], 0.7)]

    asyncio.run(retrieval.search_with_carryover([0.1] * 8, [], concepts))

    assert stub_two_collections["search_limit"] == retrieval.RETRIEVAL_TOP_K * 2


def test_empty_glossary_renders_nothing_at_all():
    """Zero tokens on the usual turn, not a "no terms matched" header."""
    assert retrieval.format_concepts_for_prompt([]) == ""


def test_glossary_block_carries_the_sql_fragment_and_tables():
    concept = retrieval.Concept(
        term="القضية المختنقة",
        aliases=["مختنقة"],
        definition="الدعوى التي تجاوزت المدة.",
        sql="case_congestion.congestion_status = 'مختنقة'",
        tables=["case_congestion"],
        score=0.73,
    )

    block = retrieval.format_concepts_for_prompt([concept])

    assert "case_congestion.congestion_status = 'مختنقة'" in block
    assert "القضية المختنقة" in block
    assert "مختنقة" in block  # alias
    assert "case_congestion" in block
