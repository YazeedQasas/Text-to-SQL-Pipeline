from typing import Literal

from pydantic import BaseModel, Field


class HistoryTurn(BaseModel):
    """One past exchange, replayed by the browser so the model has context.

    Result rows are deliberately absent: a single 200-row result outweighs the
    schema, the system prompt and the whole rest of the transcript combined.
    The answer text already says what the rows showed.
    """

    question: str = Field(..., max_length=2000)
    sql: str = Field("", max_length=4000)
    answer: str = Field("", max_length=4000)
    # Tables this turn resolved to, carried forward so a follow-up that
    # retrieves nothing on its own still sees the schema it depends on.
    table_names: list[str] = Field(default_factory=list, max_length=20)


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="Natural-language question from the user.")
    # The backend holds no session state; the transcript lives in the browser
    # and is replayed here. This cap only bounds the request body — the token
    # budget in services/context.py is what actually decides how much history
    # the model is given, and it binds first.
    history: list[HistoryTurn] = Field(default_factory=list, max_length=1000)


class RetrievedTable(BaseModel):
    table_name: str
    description: str
    score: float


class ContextUsage(BaseModel):
    """How much of the model's window this exchange occupied."""

    used_tokens: int
    limit_tokens: int
    history_turns: int


class QueryResponse(BaseModel):
    answer: str
    sql: str
    columns: list[str]
    rows: list[dict]
    retrieved_tables: list[RetrievedTable]
    usage: ContextUsage


# --- Schema catalog review ----------------------------------------------------


class ColumnDocModel(BaseModel):
    name: str
    type: str
    nullable: bool
    description: str = ""


class ForeignKeyDocModel(BaseModel):
    column: str
    references_table: str
    references_column: str


class TableDocModel(BaseModel):
    """The document that gets embedded and stored in Qdrant for one table.

    Field names match the Qdrant payload exactly, so `model_dump()` is written
    straight through by catalog.upsert_tables.
    """

    table_name: str
    description: str = ""
    primary_key: str = ""
    columns: list[ColumnDocModel] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyDocModel] = Field(default_factory=list)


class ColumnChangeModel(BaseModel):
    change_type: str
    column: str
    before: str | None = None
    after: str | None = None


class VerdictModel(BaseModel):
    needs_edit: bool
    severity: str
    reasons: list[str] = Field(default_factory=list)
    suggested_description: str = ""
    suggested_column_descriptions: dict[str, str] = Field(default_factory=dict)


class TableChangeModel(BaseModel):
    change_type: str
    table_name: str
    summary: str
    column_changes: list[ColumnChangeModel] = Field(default_factory=list)
    doc: TableDocModel
    verdict: VerdictModel
    sample_rows: list[dict] = Field(default_factory=list)
    row_count: int = -1


class ScanResponse(BaseModel):
    changes: list[TableChangeModel]
    indexed_table_count: int
    live_table_count: int


class ApproveItem(BaseModel):
    doc: TableDocModel
    action: Literal["upsert", "delete", "skip"]


class ApproveRequest(BaseModel):
    items: list[ApproveItem]


class ApproveResponse(BaseModel):
    upserted: list[str]
    deleted: list[str]
    skipped: list[str]


# --- Automated documentation (CDC) --------------------------------------------


class ReviewEntry(BaseModel):
    """A table the automated path documented, waiting to be read by a human.

    Shaped to match TableChangeModel closely enough that the Schema updates tab
    renders it with the same review card, so a table found by a Debezium event
    at 3am is dealt with exactly like one found by clicking Scan.
    """

    table_name: str
    change_type: str
    summary: str
    doc: TableDocModel
    verdict: VerdictModel
    sample_rows: list[dict] = Field(default_factory=list)
    row_count: int = -1
    # False: the reviewer flagged it, so it is NOT in Qdrant and cannot be
    # queried until a human approves it. True: it is already indexed and
    # working — this entry exists only so the description can be improved.
    indexed: bool = False
    detected_at: float = 0.0


class ReviewQueueResponse(BaseModel):
    entries: list[ReviewEntry]


class CdcStatusResponse(BaseModel):
    enabled: bool
    running: bool
    events_received: int
    # Tables that have been seen but whose debounce window has not closed yet.
    pending_tables: list[str] = Field(default_factory=list)
    debounce_seconds: float
    # Tables documented but not indexed, because the reviewer flagged them.
    # This is the number worth badging: they are not queryable yet.
    awaiting_review: int = 0
    # Tables indexed automatically and never read by anyone. Informational.
    auto_documented: int = 0


# --- Legal concepts -----------------------------------------------------------


class ConceptModel(BaseModel):
    """One glossary entry. Field names match data/concepts.json exactly."""

    id: str = Field(..., min_length=1, max_length=100)
    term: str = Field("", max_length=200)
    aliases: list[str] = Field(default_factory=list)
    definition: str = Field("", max_length=4000)
    # A SQL fragment — a predicate, an expression or a join path — never a
    # complete query. See data/CONCEPTS.md.
    sql: str = Field("", max_length=2000)
    tables: list[str] = Field(default_factory=list)


class ConceptsResponse(BaseModel):
    concepts: list[ConceptModel]
    # What Qdrant holds right now, so the UI can show the two sides without a
    # second round trip.
    qdrant_count: int = 0
    in_sync: bool = True


class ConceptUploadRequest(BaseModel):
    """A replacement concepts.json, posted from the browser.

    `force` skips the delete rail. It is the explicit second step after a sync
    was refused for wanting to delete too much.
    """

    concepts: list[ConceptModel]
    force: bool = False


class ConceptConflictModel(BaseModel):
    concept_id: str
    file_version: dict
    qdrant_version: dict


class ConceptSyncResponse(BaseModel):
    ok: bool
    trigger: str
    upserted: list[str] = Field(default_factory=list)
    deleted_from_qdrant: list[str] = Field(default_factory=list)
    added_to_file: list[str] = Field(default_factory=list)
    updated_in_file: list[str] = Field(default_factory=list)
    removed_from_file: list[str] = Field(default_factory=list)
    conflicts: list[ConceptConflictModel] = Field(default_factory=list)
    # Set when the delete rail refused the plan; the sync can be retried with
    # force once a human has read this.
    refused_reason: str = ""
    file_count: int = 0
    qdrant_count: int = 0
