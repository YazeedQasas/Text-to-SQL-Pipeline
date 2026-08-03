from typing import Literal

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="Natural-language question from the user.")


class RetrievedTable(BaseModel):
    table_name: str
    description: str
    score: float


class QueryResponse(BaseModel):
    answer: str
    sql: str
    columns: list[str]
    rows: list[dict]
    retrieved_tables: list[RetrievedTable]


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
