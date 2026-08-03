"""Read-only MySQL query execution.

Uses a synchronous PyMySQL connection run inside FastAPI's threadpool (via
run_in_threadpool) rather than an async MySQL driver — this keeps the
dependency surface small for a first working version. The connecting user
must have SELECT-only grants (db/03_readonly_user.sql); sql_guard.py is an
additional application-level check, not the primary safeguard.
"""

import pymysql
import pymysql.cursors
from fastapi.concurrency import run_in_threadpool

from app.config import (
    MYSQL_DATABASE,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_USER,
    SQL_STATEMENT_TIMEOUT_SECONDS,
)


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=5,
        read_timeout=SQL_STATEMENT_TIMEOUT_SECONDS + 2,
    )


def _execute_select_sync(sql: str, params: tuple | None = None) -> tuple[list[str], list[dict]]:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            try:
                cursor.execute(f"SET SESSION MAX_EXECUTION_TIME={SQL_STATEMENT_TIMEOUT_SECONDS * 1000}")
            except pymysql.MySQLError:
                pass  # not supported on this server (e.g. MariaDB) — best-effort only

            cursor.execute(sql, params)
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return columns, rows
    finally:
        connection.close()


async def execute_select(sql: str, params: tuple | None = None) -> tuple[list[str], list[dict]]:
    """Run a read-only SELECT.

    `params` is passed straight to PyMySQL for server-side interpolation, so
    callers with untrusted values (schema introspection filtering on database
    name, for example) never build SQL by string concatenation. Existing
    callers that pass fully-formed SQL are unaffected.
    """
    return await run_in_threadpool(_execute_select_sync, sql, params)
