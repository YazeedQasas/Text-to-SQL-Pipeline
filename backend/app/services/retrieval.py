"""Qdrant-backed schema retrieval.

Retrieves the most relevant TABLE-level schema documents for a user's
question. `level == "table"` is filtered explicitly so that a future
column-level collection (or mixed-level points in the same collection) can
be introduced without this query silently starting to return column docs
alongside table docs.
"""

from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.config import QDRANT_API_KEY, QDRANT_COLLECTION, QDRANT_URL, RETRIEVAL_TOP_K
from app.services.embeddings import embed_text

_client = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)


@dataclass
class SchemaTable:
    table_name: str
    description: str
    primary_key: str
    columns: list[dict]
    foreign_keys: list[dict]
    score: float


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

    tables = []
    for point in results:
        payload = point.payload or {}
        tables.append(
            SchemaTable(
                table_name=payload["table_name"],
                description=payload["description"],
                primary_key=payload["primary_key"],
                columns=payload["columns"],
                foreign_keys=payload["foreign_keys"],
                score=point.score,
            )
        )
    return tables


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
