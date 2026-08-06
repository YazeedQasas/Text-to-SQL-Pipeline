"""Qdrant-backed schema retrieval.

Retrieves the most relevant TABLE-level schema documents for a user's
question. `level == "table"` is filtered explicitly so that a future
column-level collection (or mixed-level points in the same collection) can
be introduced without this query silently starting to return column docs
alongside table docs.
"""

import time
from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    SearchRequest,
)

from app.config import (
    CONCEPT_BOOST_TABLES,
    CONCEPT_CACHE_TTL_SECONDS,
    CONCEPT_SCORE_THRESHOLD,
    CONCEPT_TOP_K,
    CONTEXT_CARRY_TABLES,
    QDRANT_API_KEY,
    QDRANT_COLLECTION,
    QDRANT_CONCEPTS_COLLECTION,
    QDRANT_URL,
    RETRIEVAL_TOP_K,
)
from app.services import arabic
from app.services.embeddings import embed_text

_client = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)


@dataclass
class Concept:
    """A domain term the schema does not contain, and its SQL mapping.

    See data/CONCEPTS.md. `sql` is a fragment — a predicate, an expression,
    or a join path — never a complete query.

    `matched_by` records which signal found it: "lexical" when the term is
    written in the question outright, "vector" when a fragment of the question
    embedded close to it. A lexical hit carries no meaningful similarity score,
    so `score` is 1.0 there — the field is only comparable within one signal.
    """

    term: str
    aliases: list[str]
    definition: str
    sql: str
    tables: list[str]
    score: float
    matched_by: str = "vector"


@dataclass
class SchemaTable:
    table_name: str
    description: str
    primary_key: str
    columns: list[dict]
    foreign_keys: list[dict]
    score: float


def _to_table(payload: dict, score: float) -> SchemaTable:
    return SchemaTable(
        table_name=payload["table_name"],
        description=payload["description"],
        primary_key=payload["primary_key"],
        columns=payload["columns"],
        foreign_keys=payload["foreign_keys"],
        score=score,
    )


async def retrieve_tables(question: str, top_k: int = RETRIEVAL_TOP_K) -> list[SchemaTable]:
    """Embed a question and return the schema tables closest to it."""
    return await search_tables(await embed_text(question), top_k)


async def search_tables(
    query_vector: list[float], top_k: int = RETRIEVAL_TOP_K
) -> list[SchemaTable]:
    """Vector-search half of `retrieve_tables`.

    Kept separate so the pipeline can report "embedding" and "searching" as
    distinct stages to the client.
    """
    results = await _client.search(
        collection_name=QDRANT_COLLECTION,
        query_vector=query_vector,
        limit=top_k,
        query_filter=Filter(must=[FieldCondition(key="level", match=MatchValue(value="table"))]),
        with_payload=True,
    )

    return [_to_table(point.payload or {}, point.score) for point in results]


async def fetch_tables_by_name(table_names: list[str]) -> list[SchemaTable]:
    """Load specific table documents, ignoring relevance.

    Used to carry the previous turn's tables into a follow-up question, whose
    own embedding ("and who was the judge?") retrieves nothing useful. Scored 0
    so they never outrank a table the new question actually matched.
    """
    if not table_names:
        return []

    points, _ = await _client.scroll(
        collection_name=QDRANT_COLLECTION,
        # One MatchAny rather than a `should` list: it is a single condition
        # that stays inside `must`, so it needs no min_should — whose shape has
        # differed across qdrant-client releases.
        scroll_filter=Filter(
            must=[
                FieldCondition(key="level", match=MatchValue(value="table")),
                FieldCondition(key="table_name", match=MatchAny(any=table_names)),
            ]
        ),
        limit=len(table_names),
        with_payload=True,
        with_vectors=False,
    )

    by_name = {(point.payload or {}).get("table_name"): point.payload for point in points}
    # Preserve the order the caller asked for, so the prompt is stable turn to turn.
    return [_to_table(by_name[name], 0.0) for name in table_names if by_name.get(name)]


def _to_concept(payload: dict, score: float, matched_by: str) -> Concept:
    return Concept(
        term=payload["term"],
        aliases=payload.get("aliases", []),
        definition=payload["definition"],
        sql=payload["sql"],
        tables=payload.get("tables", []),
        score=score,
        matched_by=matched_by,
    )


# The whole glossary, held in memory for lexical matching — which has to test
# every concept's surface forms against the question and so cannot be expressed
# as a vector search. Tens of concepts, refreshed on a timer so re-running
# a concept sync takes effect without a backend restart.
_concept_cache: list[dict] | None = None
_concept_cache_loaded_at = 0.0


async def load_all_concepts(force: bool = False) -> list[dict]:
    """Every concept payload, cached for CONCEPT_CACHE_TTL_SECONDS."""
    global _concept_cache, _concept_cache_loaded_at

    fresh = time.monotonic() - _concept_cache_loaded_at < CONCEPT_CACHE_TTL_SECONDS
    if _concept_cache is not None and fresh and not force:
        return _concept_cache

    if not await _client.collection_exists(QDRANT_CONCEPTS_COLLECTION):
        _concept_cache, _concept_cache_loaded_at = [], time.monotonic()
        return _concept_cache

    payloads: list[dict] = []
    offset = None
    while True:
        points, offset = await _client.scroll(
            collection_name=QDRANT_CONCEPTS_COLLECTION,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        payloads.extend(point.payload for point in points if point.payload)
        if offset is None:
            break

    _concept_cache, _concept_cache_loaded_at = payloads, time.monotonic()
    return payloads


async def search_concepts(
    question: str,
    gram_vectors: list[tuple[str, list[float]]],
    top_k: int = CONCEPT_TOP_K,
    score_threshold: float = CONCEPT_SCORE_THRESHOLD,
) -> list[Concept]:
    """Find domain terms the question uses, or nothing at all.

    Unlike schema retrieval — which must always return tables, because every
    question needs some — this is EXPECTED to return an empty list most turns.
    Most questions contain no Palestine-specific term, and injecting the nearest
    definitions anyway would hand the model irrelevant mappings to honour.

    Two signals, unioned, because they fail in different places:

    - LEXICAL: the term is written in the question. Normalized substring match,
      free, and completely unaffected by how long the question is. Over a
      15-question probe it was right 14 times, its only miss being a plural
      ("التشريعات السارية") that does not contain the singular concept.
    - VECTOR: the best-scoring fragment of the question, over the threshold.
      Catches the morphology and paraphrase cases lexical cannot, and covers
      exactly that plural. Scored per fragment rather than over the whole
      question — see arabic.content_grams for why.

    Neither separates cleanly alone; together they were 15/15 with no false
    positives. Lexical hits lead, because a term written outright is a stronger
    signal than any similarity score, and leading means they claim the schema
    boost slots first.

    A missing collection is not an error: the glossary is optional, and a
    deployment whose concepts have never been synced answers exactly as it did
    before rather than failing every question.
    """
    payloads = await load_all_concepts()
    if not payloads:
        return []

    by_term: dict[str, dict] = {p["term"]: p for p in payloads}
    matched: dict[str, Concept] = {}

    lexical_surfaces: dict[str, str] = {}
    for payload in payloads:
        surfaces = [payload["term"], *payload.get("aliases", [])]
        if (hit := arabic.lexical_match(question, surfaces)) is not None:
            lexical_surfaces[payload["term"]] = hit

    # Longest-match-wins ACROSS concepts, not just within one. "المستدعي" is a
    # substring of "المستدعى ضده" once normalized, but they are opposite party
    # roles — keeping both would hand the model role = 'مدعي' for a question
    # that asked only about the respondent.
    for term, surface in lexical_surfaces.items():
        if arabic.is_subsumed(surface, [s for t, s in lexical_surfaces.items() if t != term]):
            continue
        matched[term] = _to_concept(by_term[term], 1.0, "lexical")

    for term, score in (await _best_gram_scores(gram_vectors)).items():
        if score < score_threshold or term in matched:
            continue
        if payload := by_term.get(term):
            matched[term] = _to_concept(payload, score, "vector")

    ordered = sorted(
        matched.values(),
        # Lexical first, then by similarity within each group.
        key=lambda c: (c.matched_by != "lexical", -c.score),
    )
    return ordered[:top_k]


async def _best_gram_scores(
    gram_vectors: list[tuple[str, list[float]]],
) -> dict[str, float]:
    """Best score each concept achieved against any single question fragment.

    One batched request rather than a call per fragment: the fragments are
    independent, and a dozen round trips would cost more than the search itself.
    """
    if not gram_vectors:
        return {}

    batches = await _client.search_batch(
        collection_name=QDRANT_CONCEPTS_COLLECTION,
        requests=[
            SearchRequest(vector=vector, limit=1, with_payload=True)
            for _, vector in gram_vectors
        ],
    )

    best: dict[str, float] = {}
    for results in batches:
        for point in results:
            term = (point.payload or {}).get("term")
            if term and point.score > best.get(term, 0.0):
                best[term] = point.score
    return best


def _concept_table_names(concepts: list[Concept]) -> list[str]:
    """Tables the matched concepts need, best-scoring concept first, deduplicated."""
    names: list[str] = []
    for concept in concepts:  # already ordered by score descending
        for name in concept.tables:
            if name not in names:
                names.append(name)
    return names[:CONCEPT_BOOST_TABLES]


async def search_with_carryover(
    query_vector: list[float],
    carry_table_names: list[str],
    concepts: list[Concept] = (),
) -> list[SchemaTable]:
    """Retrieve for this question, plus the tables the last one resolved to.

    A follow-up rarely names its own subject, so retrieving on it alone drops
    the tables the conversation is actually about — and takes the prior SQL in
    the prompt out of context with it. The new question's matches lead; the
    carried tables fill in behind them, deduplicated and capped.

    Matched concepts steer WHICH tables are chosen without changing HOW MANY.
    A concept's SQL fragment names its tables, so those tables must be visible
    to the model or the fragment is unusable — but they displace weaker vector
    hits inside the same RETRIEVAL_TOP_K budget rather than stacking on top of
    it. That keeps the schema block a fixed size no matter how many concepts
    fire.
    """
    tables = await search_tables(query_vector, RETRIEVAL_TOP_K * 2 if concepts else RETRIEVAL_TOP_K)
    tables = await _apply_concept_boost(tables, _concept_table_names(list(concepts)))

    if not carry_table_names:
        return tables

    seen = {table.table_name for table in tables}
    missing = [name for name in carry_table_names if name not in seen]
    carried = await fetch_tables_by_name(missing[:CONTEXT_CARRY_TABLES])

    return tables + carried


async def _apply_concept_boost(
    tables: list[SchemaTable], boost_names: list[str]
) -> list[SchemaTable]:
    """Promote concept-named tables to the front, then truncate to the top-k budget.

    Promotion is unconditional rather than a score nudge: `tables` on a concept
    is a human asserting "this term needs this table", which is a stronger
    signal than the question's own embedding — that embedding is exactly what
    failed to find the table, or the concept would not be adding anything.

    A named table missing from the widened search is fetched by name. Without
    that, the one case this feature exists for — a concept whose table the
    question never gestures at — would still lose its table.
    """
    if not boost_names:
        return tables[:RETRIEVAL_TOP_K]

    by_name = {table.table_name: table for table in tables}
    absent = [name for name in boost_names if name not in by_name]
    for fetched in await fetch_tables_by_name(absent):
        by_name[fetched.table_name] = fetched

    promoted = [by_name[name] for name in boost_names if name in by_name]
    remainder = [table for table in tables if table.table_name not in set(boost_names)]

    return (promoted + remainder)[:RETRIEVAL_TOP_K]


def format_tables_for_prompt(tables: list[SchemaTable]) -> str:
    """Render retrieved tables (and the FKs between them) as SQL-generation context.

    Foreign keys are rendered as explicit "JOIN hints" so the LLM expresses
    cross-table relationships as JOINs instead of guessing column names.
    """
    blocks = []
    all_fk_lines = []

    for table in tables:
        lines = [f"Table: {table.table_name}", f"Description: {table.description}", "Columns:"]
        for col in table.columns:
            nullability = "NULL" if col["nullable"] else "NOT NULL"
            lines.append(f"  - {col['name']} ({col['type']}, {nullability}): {col['description']}")
        lines.append(f"Primary key: {table.primary_key}")
        blocks.append("\n".join(lines))

        for fk in table.foreign_keys:
            all_fk_lines.append(f"{table.table_name}.{fk['column']} = {fk['references_table']}.{fk['references_column']}")

    schema_section = "\n\n".join(blocks)
    if all_fk_lines:
        join_section = "Relationships (use these for JOINs):\n" + "\n".join(f"  - {line}" for line in all_fk_lines)
    else:
        join_section = "Relationships: none among the retrieved tables."

    return f"{schema_section}\n\n{join_section}"


def format_concepts_for_prompt(concepts: list[Concept]) -> str:
    """Render matched domain terms as a glossary block, or "" if none matched.

    Returns the empty string rather than a "no terms matched" header so that the
    usual turn — where nothing clears the score threshold — costs zero tokens
    and leaves the prompt byte-identical to what it was before this feature.

    The labels are English, matching `format_tables_for_prompt`, while the terms
    and definitions stay Arabic. The SQL fragment is rendered last and labelled,
    because it is the part the model must reuse verbatim; the definition sits
    above it as the justification.
    """
    if not concepts:
        return ""

    lines = [
        "Domain terms used in this question. Each maps the user's wording to the "
        "schema; reuse the SQL fragment as written, as PART of your query — it is "
        "a predicate or expression, never a complete statement."
    ]
    for concept in concepts:
        aliases = f" (also: {'، '.join(concept.aliases)})" if concept.aliases else ""
        lines.append(f"\n- {concept.term}{aliases}: {concept.definition}")
        lines.append(f"  Tables: {', '.join(concept.tables)}")
        lines.append(f"  SQL: {concept.sql}")

    return "\n".join(lines)
