"""Application-level guard rejecting anything but a single read-only SELECT.

This is defense in depth, not the real guardrail — the real guardrail is
that the backend connects to MySQL as a user with SELECT-only grants (see
db/03_readonly_user.sql), so even a guard bypass or bug here cannot result in
a write. This layer exists to fail fast with a clear error instead of
relying on the database to reject the statement.
"""

import re

import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import DDL, DML

from app.config import MAX_RESULT_ROWS

_FORBIDDEN_DML_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE", "CALL",
}
_FORBIDDEN_DDL_KEYWORDS = {
    "CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME",
}
_FORBIDDEN_OTHER_KEYWORDS = {
    "GRANT", "REVOKE", "SET", "LOCK", "UNLOCK", "EXECUTE", "PREPARE",
    "LOAD", "OUTFILE", "INFILE", "ATTACH", "DETACH",
}
_FORBIDDEN_KEYWORDS = _FORBIDDEN_DML_KEYWORDS | _FORBIDDEN_DDL_KEYWORDS | _FORBIDDEN_OTHER_KEYWORDS

_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)


class SQLGuardError(ValueError):
    pass


def _statement_type(statement: Statement) -> str | None:
    for token in statement.tokens:
        if token.ttype in (DML, DDL):
            return token.value.upper()
        if not token.is_whitespace and token.ttype is None and token.value.strip():
            # Non-keyword leading token (e.g. a comment or paren) — keep scanning.
            continue
    return None


def enforce_select_only(sql: str) -> str:
    """Validate that `sql` is exactly one read-only SELECT statement.

    Raises SQLGuardError on anything else. On success, returns the statement
    with a LIMIT clause enforced (appending one if the LLM didn't include it)
    so a single generated query can't return unbounded rows.
    """
    if not sql or not sql.strip():
        raise SQLGuardError("Generated SQL is empty.")

    raw = sql.strip().rstrip(";").strip()

    statements = sqlparse.parse(raw)
    statements = [s for s in statements if s.token_first(skip_cm=True) is not None]
    if len(statements) != 1:
        raise SQLGuardError(f"Expected exactly one SQL statement, got {len(statements)}.")

    statement = statements[0]

    stmt_type = _statement_type(statement)
    if stmt_type != "SELECT":
        raise SQLGuardError(f"Only SELECT statements are allowed (got: {stmt_type or 'unknown'}).")

    upper_sql = raw.upper()
    for keyword in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", upper_sql):
            raise SQLGuardError(f"Forbidden keyword detected: {keyword}")

    if ";" in raw:
        raise SQLGuardError("Multiple statements are not allowed.")

    if "--" in raw or "/*" in raw or "#" in raw:
        raise SQLGuardError("SQL comments are not allowed.")

    if not _LIMIT_RE.search(raw):
        raw = f"{raw} LIMIT {MAX_RESULT_ROWS}"

    return raw
