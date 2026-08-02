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
