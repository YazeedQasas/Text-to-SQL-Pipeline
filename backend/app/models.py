from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    """A question, and where its conversation comes from.

    There are two ways to supply that conversation, and exactly one may be used
    per request:

      `chat_id`  — the turns are read from the store, and this turn is appended
                   to it. The persistent path, and what the UI uses.
      `history`  — the turns are supplied in the body and nothing is saved. The
                   original stateless path, kept for one-off and scripted calls
                   that have no chat behind them.

    Sending both is rejected rather than resolved by precedence. Whichever one
    lost would be silently ignored, which is how a client ends up believing a
    conversation the model was never shown.
    """

    question: str = Field(..., min_length=1, max_length=2000, description="Natural-language question from the user.")
    # A stored conversation (db/07_app_schema.sql). Each chat's context window is
    # measured from its own turns, so two chats can never share a budget.
    chat_id: str | None = Field(None, min_length=36, max_length=36)
    # The stateless alternative: the transcript replayed in the request body.
    # This cap only bounds the body — the token budget in services/context.py is
    # what decides how much history the model is given, and it binds first.
    history: list[HistoryTurn] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def _single_source_of_history(self) -> "QueryRequest":
        if self.chat_id is not None and self.history:
            raise ValueError(
                "Send either chat_id or history, not both: the stored conversation and "
                "the one in this body would disagree, and only one can reach the model."
            )
        return self


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
    # Whether this turn reached the store. False on the stateless path, which has
    # no chat to save to, and false when the write failed — a MySQL blip, or a
    # chat deleted while the answer was being written.
    #
    # Reported rather than swallowed because the failure is invisible otherwise:
    # the answer is on screen, so the conversation looks intact until a reload
    # shows the turn was never there. The client can say so at the time.
    saved: bool = False


# --- Chats --------------------------------------------------------------------


class ChatModel(BaseModel):
    """One conversation, as the sidebar sees it.

    No context-window figure here. How full a chat is depends on the schema
    retrieved for its next question, so it is a property of a request rather
    than of the chat — `ContextUsage` reports it per turn.
    """

    id: str
    # NULL until the first turn names it. The client shows a placeholder rather
    # than inventing a title, so an empty chat never looks like a titled one.
    title: str | None = None
    created_at: float
    updated_at: float
    turn_count: int = 0


class ChatListResponse(BaseModel):
    chats: list[ChatModel]


class CreateChatRequest(BaseModel):
    """`id` is optional and client-generated when present.

    The browser can mint a UUID, key its local state on it and start streaming
    without waiting for this call to come back. Sending an id that already exists
    returns that chat unchanged rather than erroring, so a retry after a dropped
    connection is safe.
    """

    id: str | None = Field(None, min_length=36, max_length=36)


class RenameChatRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)


class ChatTurnModel(BaseModel):
    """A stored turn, as the transcript is redrawn from it.

    Wider than HistoryTurn by exactly one field — the row id — because the client
    needs a stable React key. Result rows are absent for the same reason they are
    absent from HistoryTurn, which has a visible consequence: reopening a chat
    shows every answer but no result tables. The answer text carries what the
    rows said.
    """

    id: int
    question: str
    sql: str = ""
    answer: str = ""
    table_names: list[str] = Field(default_factory=list)
    created_at: float


class ChatDetailResponse(BaseModel):
    """A chat and its transcript, so opening one costs a single round trip."""

    chat: ChatModel
    turns: list[ChatTurnModel]


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
