"""Azure SQL connections.

Deliberately small. The previous implementation's connection pool was the
best-engineered file in it - correct handling of pyodbc's thread affinity, a
liveness check before reuse, rollback on return - and this keeps those ideas
while adding the one it lacked: retry with backoff on the transient faults
Azure SQL raises routinely.

A connection is never shared across threads. pyodbc connections are not
thread-safe, and the failure mode when they are shared is corrupted result
sets rather than an exception, which is the worst kind.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pyodbc

from sbh_data_bank.store.config import SqlSettings

__all__ = ["Database", "TransientSqlError", "connect"]

log = logging.getLogger(__name__)

#: Azure SQL raises these while failing over, throttling, or when a database is
#: resuming from pause. All are worth retrying; nothing else is.
TRANSIENT_SQLSTATES = frozenset({"08S01", "08001", "40001", "40197", "40501", "40613", "HYT00"})
TRANSIENT_MESSAGES = (
    "deadlock",
    "timeout expired",
    "transport-level error",
    "is not currently available",
    "please retry",
)


class TransientSqlError(RuntimeError):
    """A fault worth retrying, raised after the retries were exhausted."""


def _is_transient(exc: Exception) -> bool:
    state = getattr(exc, "args", [None])[0]
    if isinstance(state, str) and state in TRANSIENT_SQLSTATES:
        return True
    text = str(exc).lower()
    return any(m in text for m in TRANSIENT_MESSAGES)


def connect(settings: SqlSettings, *, attempts: int = 4) -> pyodbc.Connection:
    """Open a connection, retrying transient faults with backoff.

    The connection string is never included in an error message: it carries the
    password.
    """
    delay = 1.0
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            connection = pyodbc.connect(
                settings.connection_string(), timeout=settings.login_timeout
            )
            connection.autocommit = False
            return connection
        except pyodbc.Error as exc:
            last = exc
            if not _is_transient(exc) or attempt == attempts:
                raise TransientSqlError(
                    f"could not connect to {settings.describe()}: {exc}"
                ) from exc
            log.warning(
                "transient connection failure to %s (attempt %d/%d), retrying in %.1fs",
                settings.describe(),
                attempt,
                attempts,
                delay,
            )
            time.sleep(delay)
            delay *= 2
    raise TransientSqlError(f"could not connect to {settings.describe()}: {last}")


class Database:
    """A single-threaded handle onto one database.

    Not a pool. A load runs one file at a time against one connection, and
    concurrency belongs at the file level - several processes, each with its own
    connection - rather than inside one.
    """

    __slots__ = ("_connection", "settings")

    def __init__(self, settings: SqlSettings) -> None:
        self.settings = settings
        self._connection: pyodbc.Connection | None = None

    def __enter__(self) -> Database:
        self._connection = connect(self.settings)
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def connection(self) -> pyodbc.Connection:
        if self._connection is None:
            self._connection = connect(self.settings)
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    @contextmanager
    def transaction(self) -> Iterator[pyodbc.Cursor]:
        """One unit of work, committed together or not at all.

        A file's rows, its quarantine records and its batch record commit as one
        transaction. A load that fails part way leaves nothing behind, which is
        what makes a failed run safe to simply re-run.
        """
        cursor = self.connection.cursor()
        try:
            yield cursor
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def execute(self, sql: str, *params: Any) -> list[tuple]:
        """Run a statement and return all rows. For small reads and DDL."""
        with self.transaction() as cursor:
            cursor.execute(sql, *params)
            if cursor.description is None:
                return []
            return [tuple(r) for r in cursor.fetchall()]

    def scalar(self, sql: str, *params: Any) -> Any:
        rows = self.execute(sql, *params)
        return rows[0][0] if rows else None

    def exists(self, schema: str, table: str) -> bool:
        return bool(
            self.scalar(
                "SELECT COUNT(*) FROM sys.tables t JOIN sys.schemas s "
                "ON s.schema_id = t.schema_id WHERE s.name = ? AND t.name = ?",
                schema,
                table,
            )
        )
