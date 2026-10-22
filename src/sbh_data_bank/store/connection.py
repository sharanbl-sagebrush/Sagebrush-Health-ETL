"""Azure SQL connections.

Deliberately small. The previous implementation's connection pool was the
best-engineered file in it - correct handling of pyodbc's thread affinity, a
liveness check before reuse, rollback on return - and this keeps those ideas
while adding the one it lacked: retry with backoff on the transient faults
Azure SQL raises routinely.

A connection is never shared across threads. pyodbc connections are not
thread-safe, and the failure mode when they are shared is corrupted result
sets rather than an exception, which is the worst kind.

How this fits
-------------
``Database`` is the one object that talks to Azure SQL. ``scripts/migrate.py``
opens one to apply the DDL from ``store.schema``; ``commit.sql.SqlStore`` uses
``Database.transaction()`` to write a ``FileLoad`` atomically.
Settings come from ``store.config.SqlSettings``. Nothing in ``ingest`` or
``registry`` imports this module, which is why the unit tests need no server.

Note on the docstring above: this module is *not* a pool. There is no idle
list and no liveness probe before reuse; those ideas from the old code are
described as the lineage, and what survives here is one connection per
``Database``, rollback on any failure, and retry-with-backoff on connect.
"""

from __future__ import annotations

import contextlib
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
#:   08S01  communication link failure     08001  client unable to connect
#:   40001  deadlock victim                40197  service error, retry
#:   40501  service busy (throttled)       40613  database not currently
#:                                                available (paused/failover)
#:   HYT00  login timeout expired
TRANSIENT_SQLSTATES = frozenset({"08S01", "08001", "40001", "40197", "40501", "40613", "HYT00"})
#: Message fragments for the same faults when the SQLSTATE is not the first
#: argument of the exception (driver versions differ in how they package it).
TRANSIENT_MESSAGES = (
    "deadlock",
    "timeout expired",
    "transport-level error",
    "is not currently available",
    "please retry",
)


class TransientSqlError(RuntimeError):
    """A fault worth retrying, raised after the retries were exhausted.

    Also raised, with the same message shape, for a *non*-transient connect
    failure (bad credentials, unknown server): see :func:`connect`. The name
    describes the retry path; the message says what actually happened.
    """


def is_transient(exc: Exception) -> bool:
    """Whether a pyodbc error is one of the retryable kinds.

    pyodbc puts the SQLSTATE in ``exc.args[0]``; check that first, then fall
    back to a substring search of the message for drivers that package the
    state differently. Anything else - a syntax error, a permission failure -
    is permanent and must not be retried.
    """
    state = getattr(exc, "args", [None])[0]
    if isinstance(state, str) and state in TRANSIENT_SQLSTATES:
        return True
    text = str(exc).lower()
    return any(m in text for m in TRANSIENT_MESSAGES)


def connect(settings: SqlSettings, *, attempts: int = 4) -> pyodbc.Connection:
    """Open a connection, retrying transient faults with backoff.

    The connection string is never included in an error message: it carries the
    password.

    Returns an open connection with ``autocommit`` off, so every write happens
    inside an explicit transaction. Raises :class:`TransientSqlError` when the
    fault is permanent, or when ``attempts`` transient failures have been
    exhausted. Backoff doubles from one second: 1, 2, 4 seconds between four
    attempts, which covers the typical Azure SQL resume-from-pause window.
    """
    delay = 1.0
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            connection = pyodbc.connect(
                settings.connection_string(), timeout=settings.login_timeout
            )
            # Explicit transactions only. A load must commit its rows, its
            # quarantine and its batch record together or not at all.
            connection.autocommit = False
            return connection
        except pyodbc.Error as exc:
            last = exc
            # Give up immediately on a permanent fault, or on the last try.
            # settings.describe() names the target without the credential.
            if not is_transient(exc) or attempt == attempts:
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
    # Unreachable in practice (the loop either returns or raises), kept so the
    # function has a definite end for the type checker and for attempts=0.
    raise TransientSqlError(f"could not connect to {settings.describe()}: {last}")


class Database:
    """A single-threaded handle onto one database.

    Not a pool. A load runs one file at a time against one connection, and
    concurrency belongs at the file level - several processes, each with its own
    connection - rather than inside one.

    Usable as a context manager (``with Database(settings) as db:``) or lazily:
    the connection is opened on first use of ``connection`` and closed by
    ``close()``.
    """

    __slots__ = ("_connection", "settings")

    def __init__(self, settings: SqlSettings) -> None:
        self.settings = settings
        self._connection: pyodbc.Connection | None = None

    def __enter__(self) -> Database:
        """Open the connection eagerly so a bad configuration fails here."""
        self._connection = connect(self.settings)
        return self

    def __exit__(self, *exc: object) -> None:
        """Close on the way out. Any open transaction was already rolled back
        by ``transaction()``, so nothing is left half-committed."""
        self.close()

    @property
    def connection(self) -> pyodbc.Connection:
        """The live connection, opened on first access."""
        if self._connection is None:
            self._connection = connect(self.settings)
        return self._connection

    def close(self) -> None:
        """Close the connection if open. Safe to call twice: the ``finally``
        clears the handle even if ``close()`` itself raises."""
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

        Yields a cursor. Commits when the ``with`` block exits normally; rolls
        back and re-raises on any exception; closes the cursor either way.
        """
        cursor = self.connection.cursor()
        try:
            yield cursor
            self.connection.commit()
        except Exception:
            # Rollback-on-failure is the property the old pool got right and
            # this keeps: the connection is never left with a dangling open
            # transaction that the next unit of work would silently join.
            try:
                self.connection.rollback()
            except Exception:  # noqa: BLE001 - the link is gone; the server rolled back
                # Azure closed the connection mid-statement (2026-10-21, a
                # 200,000-row batch): the rollback cannot be sent, the server
                # has already discarded the transaction, and what matters is
                # that the next unit of work gets a fresh connection rather
                # than this dead one. The original error is the one to raise,
                # and closing a dead handle may itself fail, so that is
                # suppressed too; close() clears the handle regardless.
                with contextlib.suppress(Exception):
                    self.close()
            raise
        finally:
            with contextlib.suppress(Exception):  # a cursor on a dead connection
                cursor.close()

    def execute(self, sql: str, *params: Any) -> list[tuple]:
        """Run a statement and return all rows. For small reads and DDL.

        Each call is its own transaction. ``cursor.description`` is ``None``
        for a statement that returns no result set (DDL, INSERT), in which
        case an empty list comes back rather than a fetch error.
        """
        with self.transaction() as cursor:
            cursor.execute(sql, *params)
            if cursor.description is None:
                return []
            return [tuple(r) for r in cursor.fetchall()]

    def scalar(self, sql: str, *params: Any) -> Any:
        """First column of the first row, or ``None`` when there are no rows."""
        rows = self.execute(sql, *params)
        return rows[0][0] if rows else None

    def exists(self, schema: str, table: str) -> bool:
        """Whether ``schema.table`` exists, via the catalog views. Used by the
        migration script to report what it created versus what was already
        there."""
        return bool(
            self.scalar(
                "SELECT COUNT(*) FROM sys.tables t JOIN sys.schemas s "
                "ON s.schema_id = t.schema_id WHERE s.name = ? AND t.name = ?",
                schema,
                table,
            )
        )
