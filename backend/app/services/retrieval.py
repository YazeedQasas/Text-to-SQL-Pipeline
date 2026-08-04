"""Qdrant-backed schema retrieval.

Retrieves the most relevant TABLE-level schema documents for a user's
question. `level == "table"` is filtered explicitly so that a future
column-level collection (or mixed-level points in the same collection) can
be introduced without this query silently starting to return column docs
alongside table docs.
"""

from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from app.config import (
    CONCEPT_BOOST_TABLES,
    CONCEPT_SCORE_THRESHOLD,
    CONCEPT_TOP_K,
    CONTEXT_CARRY_TABLES,
    QDRANT_API_KEY,
    QDRANT_COLLECTION,
    QDRANT_CONCEPTS_COLLECTION,
    QDRANT_URL,
    RETRIEVAL_TOP_K,
)
from app.services.embeddings import embed_text

_client = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)


@dataclass
class Concept:
    """A domain term the schema does not contain, and its SQL mapping.

    See ingestion/concepts.py. `sql` is a fragment — a predicate, an expression,
    or a join path — never a complete query.
    """

    term: str
    aliases: list[str]
    definition: str
    sql: str
    tables: list[str]
    score: float


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


async def search_concepts(
    query_vector: list[float],
    top_k: int = CONCEPT_TOP_K,
    score_threshold: float = CONCEPT_SCORE_THRESHOLD,
) -> list[Concept]:
    """Find domain terms the question uses, or nothing at all.

    Unlike schema retrieval — which must always return tables, because every
    question needs some — this search is EXPECTED to return an empty list most
    turns. Most questions contain no Palestine-specific term, and injecting the
    three nearest definitions anyway would hand the model irrelevant mappings to
    honour. `score_threshold` is what makes abstaining the default; see the
    calibration note on CONCEPT_SCORE_THRESHOLD in config.py.

    A missing collection is not an error: the glossary is optional, and a
    deployment that has not run ingest_concepts.py should answer questions
    exactly as it did before rather than fail every one of them.
    """
    if not await _client.collection_exists(QDRANT_CONCEPTS_COLLECTION):
        return []

    results = await _client.search(
        collection_name=QDRANT_CONCEPTS_COLLECTION,
        query_vector=query_vector,
        limit=top_k,
        score_threshold=score_threshold,
        with_payload=True,
    )

    return [
        Concept(
            term=payload["term"],
            aliases=payload.get("aliases", []),
            definition=payload["definition"],
            sql=payload["sql"],
            tables=payload.get("tables", []),
            score=point.score,
        )
        for point in results
        if (payload := point.payload or {})
    ]


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
