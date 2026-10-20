"""The Azure SQL store: T-SQL for the tables ``store.schema`` generates.

Statement text is built from the spec by the module-level functions, so a
test can hold an INSERT's column list against the CREATE TABLE it targets
without a database. :class:`SqlStore` runs them through the cursor that
``store.connection.Database.transaction()`` yields; it holds that cursor
only for the duration of one transaction.

Two deliberate choices. Every row is written with a parameterised INSERT,
never by formatting values into SQL: the only text pasted into a statement
is a column name from a spec, in square brackets. And bulk inserts use
``executemany`` with ``fast_executemany`` off by default: the fast path
guesses parameter types from the first row and has a history of truncating
``DECIMAL`` scale and mis-sizing ``NVARCHAR(MAX)``; it is a flag to turn on
once it has been proved against the real tables.

The live-row query for one delivery stages the incoming identities in a
temp table and joins, rather than building an ``IN`` list: a McKesson
history file has 6,000 identities and SQL Server stops at 2,100 parameters.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from sbh_data_bank.commit.records import (
    BatchRecord,
    CurrentQuery,
    FoundFile,
    RunRecord,
    SourceFileRecord,
    StoredCurrent,
    WriteBatch,
    WriteContext,
    iter_values,
)
from sbh_data_bank.curate.rule import CurrentRow
from sbh_data_bank.registry.spec import SourceSpec
from sbh_data_bank.store.schema import CURATED_LEDGER_COLUMNS, DEDUP_RULE, table_name

__all__ = [
    "SqlStore",
    "curated_columns",
    "curated_ledger_columns",
    "current_rows_sql",
    "insert_sql",
    "landed_columns",
    "supersede_sql",
    "typed_columns",
]


class _Cursor(Protocol):
    """The slice of ``pyodbc.Cursor`` this module uses."""

    rowcount: int
    fast_executemany: bool

    def execute(self, sql: str, *params: Any) -> Any: ...
    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> Any: ...
    def fetchone(self) -> Any: ...
    def fetchall(self) -> list[Any]: ...


class _Transactional(Protocol):
    """``store.connection.Database``, or anything else whose ``transaction()``
    yields a cursor and commits on a clean exit."""

    def transaction(self) -> Any: ...


# ---------------------------------------------------------------- columns


def curated_ledger_columns() -> list[str]:
    """The ledger column names, in the order ``CURATED_LEDGER_COLUMNS`` declares
    them, parsed from that DDL fragment so the two cannot drift."""
    return [line.split()[0] for line in CURATED_LEDGER_COLUMNS.splitlines() if line.strip()]


_AUDIT = ["created_at", "created_by", "updated_at", "updated_by"]


def landed_columns(spec: SourceSpec) -> list[str]:
    """Every column of ``landed.<source>`` in DDL order."""
    return [
        "tenant_id", "source_file_id", "line_number", *_AUDIT, *spec.landed_targets, "extra_cells",
    ]  # fmt: skip


def typed_columns(spec: SourceSpec) -> list[str]:
    """Every column of ``typed.<source>`` in DDL order."""
    return [
        "tenant_id", "source_file_id", "line_number", "spec_sha256", "row_hash",
        *_AUDIT, *spec.landed_targets,
    ]  # fmt: skip


def curated_columns(spec: SourceSpec) -> list[str]:
    """Every column of ``curated.<source>`` in DDL order."""
    return [*curated_ledger_columns(), *spec.landed_targets]


def _quote(column: str) -> str:
    return f"[{column}]"


def insert_sql(table: str, columns: Sequence[str]) -> str:
    """A parameterised INSERT with one ``?`` per column."""
    names = ", ".join(_quote(c) for c in columns)
    marks = ", ".join("?" for _ in columns)
    return f"INSERT INTO {table} ({names}) VALUES ({marks})"


def supersede_sql(spec: SourceSpec) -> str:
    """Retire one live curated row by its primary key. The ``superseded_by
    IS NULL`` guard makes the rowcount the proof: 0 means the row was not
    live and the store refuses to continue."""
    return (
        f"UPDATE {table_name(spec, 'curated')} SET superseded_by = ?, superseded_at = ?, "
        "supersede_reason = ?, updated_at = ?, updated_by = ? "
        "WHERE batch_id = ? AND source_file_id = ? AND line_number = ? "
        "AND superseded_by IS NULL"
    )


_INCOMING_CREATE = (
    "CREATE TABLE #incoming (identity_hash CHAR(64) NOT NULL, occurrence INT NOT NULL, "
    "PRIMARY KEY (identity_hash, occurrence))"
)
_INCOMING_INSERT = "INSERT INTO #incoming (identity_hash, occurrence) VALUES (?, ?)"
_INCOMING_DROP = "DROP TABLE #incoming"
_CURRENT_FIXED = (
    "c.batch_id", "c.source_file_id", "c.line_number", "c.identity_hash",
    "c.occurrence", "c.row_hash", "c.as_of",
)  # fmt: skip


def current_rows_sql(spec: SourceSpec, query: CurrentQuery) -> tuple[str, list[Any]]:
    """The SELECT for the live rows a delivery is decided against, and its
    parameters. Fixed columns first, then the event, scope and control
    columns in the order :func:`_current_row` reads them."""
    extra = []
    if query.event_column and not query.snapshot_as_of_is_event_date:
        extra.append(query.event_column)
    extra += list(query.scope_columns) + list(query.control_columns)
    select = ", ".join([*_CURRENT_FIXED, *(f"c.{_quote(c)}" for c in extra)])
    where = ["c.tenant_id = ?", "c.superseded_by IS NULL"]
    params: list[Any] = []
    identity = (
        "EXISTS (SELECT 1 FROM #incoming AS i WHERE i.identity_hash = c.identity_hash "
        "AND i.occurrence = c.occurrence)"
    )
    if query.coverage is not None and query.coverage_column is not None:
        column = f"c.{_quote(query.coverage_column)}"
        window = f"{column} <= ?"
        params.append(query.coverage.end)
        if query.coverage.start is not None:
            window = f"{column} >= ? AND {window}"
            params.insert(0, query.coverage.start)
        where.append(f"({identity} OR ({window}))")
    else:
        where.append(identity)
    sql = f"SELECT {select} FROM {table_name(spec, 'curated')} AS c WHERE " + " AND ".join(where)
    return sql, params


def _current_row(record: Sequence[Any], query: CurrentQuery) -> StoredCurrent:
    batch_id, file_id, line, identity_hash, occurrence, row_hash, as_of = record[:7]
    rest = list(record[7:])
    event_date = as_of if query.snapshot_as_of_is_event_date else None
    if query.event_column and not query.snapshot_as_of_is_event_date:
        event_date = rest.pop(0)
    n_scope = len(query.scope_columns)
    scope = tuple(rest[:n_scope])
    controls = tuple(v if isinstance(v, Decimal) else Decimal(0) for v in rest[n_scope:])
    return StoredCurrent(
        identity_hash,
        occurrence,
        CurrentRow(
            row_hash=row_hash,
            as_of=as_of,
            delivery=batch_id,
            line_number=line,
            event_date=event_date,
            scope=scope,
            controls=controls,
            ref=(batch_id, file_id, line),
        ),
    )


# ------------------------------------------------------------------ meta SQL

_RUN_INSERT = insert_sql(
    "meta.run",
    ["run_id", "tenant_id", "trigger_kind", "started_at", "finished_at", "outcome", "detail"],
)
_RUN_FINISH = "UPDATE meta.run SET finished_at = ?, outcome = ?, detail = ? WHERE run_id = ?"

_SOURCE_FILE_COLUMNS = [
    "tenant_id", "source_key", "file_name", "file_sha256", "file_bytes", "sheet_name",
    "delivery", "delivery_rank", "first_seen_at", "as_of", "as_of_source",
    "coverage_from", "coverage_to", "header_json",
]  # fmt: skip
_SOURCE_FILE_INSERT = (
    f"INSERT INTO meta.source_file ({', '.join(_SOURCE_FILE_COLUMNS)}) "
    f"OUTPUT INSERTED.source_file_id VALUES ({', '.join('?' for _ in _SOURCE_FILE_COLUMNS)})"
)
_SOURCE_FILE_FIND = (
    "SELECT sf.source_file_id, "
    "(SELECT COUNT(*) FROM meta.batch AS b WHERE b.source_file_id = sf.source_file_id "
    "AND b.outcome = 'LOADED'), "
    "(SELECT COUNT(*) FROM meta.batch AS b WHERE b.source_file_id = sf.source_file_id) "
    "FROM meta.source_file AS sf WHERE sf.tenant_id = ? AND sf.file_sha256 = ? "
    "AND COALESCE(sf.sheet_name, '') = ?"
)
_DELIVERY_RANK = (
    "SELECT COUNT(*) + 1 FROM meta.source_file WHERE tenant_id = ? AND source_key = ? "
    "AND as_of IS NOT NULL AND as_of < ?"
)

_BATCH_COLUMNS = [
    "batch_id", "tenant_id", "run_id", "source_key", "source_file_id", "variant_name",
    "dedup_mode", "outcome", "detail", "rows_landed", "rows_read", "rows_inserted",
    "rows_superseded", "rows_unchanged", "rows_late", "rows_quarantined", "rows_conflict",
    "rows_vanished", "control_totals", "started_at", "finished_at",
]  # fmt: skip
_BATCH_INSERT = insert_sql("meta.batch", _BATCH_COLUMNS)
_BATCH_UPDATED = [c for c in _BATCH_COLUMNS if c not in ("batch_id", "tenant_id", "run_id")]
_BATCH_FINISH = (
    "UPDATE meta.batch SET " + ", ".join(f"{c} = ?" for c in _BATCH_UPDATED) + " WHERE batch_id = ?"
)

_QUARANTINE_INSERT = insert_sql(
    "stg.quarantine",
    [
        "tenant_id",
        "batch_id",
        "source_key",
        "source_file_id",
        "line_number",
        "reasons",
        "errors",
        "payload",
        "detected_at",
    ],  # fmt: skip
)
_FINDING_INSERT = insert_sql(
    "stg.duplicate_finding",
    [
        "tenant_id",
        "batch_id",
        "source_key",
        "source_file_id",
        "key_values",
        "line_numbers",
        "identical",
        "detected_at",
    ],  # fmt: skip
)


def _batch_values(batch: BatchRecord) -> dict[str, Any]:
    return {
        "batch_id": batch.batch_id,
        "tenant_id": batch.tenant_id,
        "run_id": batch.run_id,
        "source_key": batch.source_key,
        "source_file_id": batch.source_file_id,
        "variant_name": batch.variant_name,
        "dedup_mode": batch.dedup_mode,
        "outcome": batch.outcome,
        "detail": batch.detail,
        "rows_landed": batch.rows_landed,
        "rows_read": batch.rows_read,
        "rows_inserted": batch.rows_inserted,
        "rows_superseded": batch.rows_superseded,
        "rows_unchanged": batch.rows_unchanged,
        "rows_late": batch.rows_late,
        "rows_quarantined": batch.rows_quarantined,
        "rows_conflict": batch.rows_conflict,
        "rows_vanished": batch.rows_vanished,
        "control_totals": batch.control_totals,
        "started_at": batch.started_at,
        "finished_at": batch.finished_at,
    }


# ------------------------------------------------------------------ the store


class SqlStore:
    """Implements ``records.Store`` over a ``Database``-like object."""

    def __init__(self, database: _Transactional, *, fast_executemany: bool = False) -> None:
        self._db = database
        self._fast = fast_executemany
        self._cursor: _Cursor | None = None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One ``Database.transaction()``: the cursor it yields serves every
        statement until the block ends, then it is committed or rolled back
        by the connection."""
        with self._db.transaction() as cursor:
            cursor.fast_executemany = self._fast
            self._cursor = cursor
            try:
                yield
            finally:
                self._cursor = None

    @property
    def cursor(self) -> _Cursor:
        if self._cursor is None:
            raise RuntimeError("SqlStore is used outside a transaction")
        return self._cursor

    # --------------------------------------------------------------- meta

    def insert_run(self, run: RunRecord) -> None:
        self.cursor.execute(
            _RUN_INSERT,
            run.run_id, run.tenant_id, run.trigger_kind, run.started_at,
            run.finished_at, run.outcome, run.detail,
        )  # fmt: skip

    def finish_run(self, run: RunRecord) -> None:
        self.cursor.execute(_RUN_FINISH, run.finished_at, run.outcome, run.detail, run.run_id)

    def find_source_file(
        self, tenant_id: str, file_sha256: str, sheet_name: str | None
    ) -> FoundFile | None:
        self.cursor.execute(_SOURCE_FILE_FIND, tenant_id, file_sha256, sheet_name or "")
        row = self.cursor.fetchone()
        if row is None:
            return None
        return FoundFile(int(row[0]), int(row[1]) > 0, int(row[2]))

    def insert_source_file(self, record: SourceFileRecord) -> int:
        self.cursor.execute(
            _SOURCE_FILE_INSERT,
            record.tenant_id, record.source_key, record.file_name, record.file_sha256,
            record.file_bytes, record.sheet_name, record.delivery, record.delivery_rank,
            record.first_seen_at, record.as_of, record.as_of_source, record.coverage_from,
            record.coverage_to, record.header_json,
        )  # fmt: skip
        row = self.cursor.fetchone()
        file_id = int(row[0])
        record.source_file_id = file_id
        return file_id

    def delivery_rank(self, tenant_id: str, source_key: str, as_of: date) -> int:
        self.cursor.execute(_DELIVERY_RANK, tenant_id, source_key, as_of)
        return int(self.cursor.fetchone()[0])

    def insert_batch(self, batch: BatchRecord) -> None:
        values = _batch_values(batch)
        self.cursor.execute(_BATCH_INSERT, *(values[c] for c in _BATCH_COLUMNS))

    def finish_batch(self, batch: BatchRecord) -> None:
        values = _batch_values(batch)
        self.cursor.execute(_BATCH_FINISH, *(values[c] for c in _BATCH_UPDATED), batch.batch_id)

    # ------------------------------------------------------------ curated

    def current_rows(
        self, spec: SourceSpec, tenant_id: str, query: CurrentQuery
    ) -> list[StoredCurrent]:
        sql, params = current_rows_sql(spec, query)
        cursor = self.cursor
        cursor.execute(_INCOMING_CREATE)
        try:
            if query.keys:
                cursor.executemany(_INCOMING_INSERT, sorted(query.keys))
            cursor.execute(sql, tenant_id, *params)
            return [_current_row(tuple(r), query) for r in cursor.fetchall()]
        finally:
            cursor.execute(_INCOMING_DROP)

    # ------------------------------------------------------------- writes

    def write(self, spec: SourceSpec, ctx: WriteContext, batch: WriteBatch) -> None:
        cursor = self.cursor
        audit = (ctx.created_at, ctx.created_by, None, None)
        targets = list(spec.landed_targets)

        if batch.landed:
            cursor.executemany(
                insert_sql(table_name(spec, "landed"), landed_columns(spec)),
                [
                    (
                        ctx.tenant_id,
                        ctx.source_file_id,
                        r.line_number,
                        *audit,
                        *iter_values(r.cells, targets),
                        r.extra_cells,
                    )  # fmt: skip
                    for r in batch.landed
                ],
            )
        if batch.typed:
            cursor.executemany(
                insert_sql(table_name(spec, "typed"), typed_columns(spec)),
                [
                    (
                        ctx.tenant_id,
                        ctx.source_file_id,
                        r.line_number,
                        r.spec_sha256,
                        r.row_hash,
                        *audit,
                        *iter_values(r.values, targets),
                    )  # fmt: skip
                    for r in batch.typed
                ],
            )
        if batch.curated:
            cursor.executemany(
                insert_sql(table_name(spec, "curated"), curated_columns(spec)),
                [
                    (
                        ctx.tenant_id,
                        ctx.batch_id,
                        ctx.source_file_id,
                        r.line_number,
                        DEDUP_RULE,
                        r.identity_hash,
                        r.occurrence,
                        r.row_hash,
                        r.as_of,
                        r.outcome,
                        ctx.created_at,
                        ctx.created_by,
                        r.superseded_by,
                        r.superseded_at,
                        r.supersede_reason,
                        ctx.created_at if r.superseded_by else None,
                        ctx.created_by if r.superseded_by else None,
                        *iter_values(r.values, targets),
                    )  # fmt: skip
                    for r in batch.curated
                ],
            )
        if batch.quarantine:
            cursor.executemany(
                _QUARANTINE_INSERT,
                [
                    (
                        ctx.tenant_id,
                        ctx.batch_id,
                        spec.key,
                        ctx.source_file_id,
                        q.line_number,
                        q.reasons,
                        q.errors,
                        q.payload,
                        ctx.created_at,
                    )  # fmt: skip
                    for q in batch.quarantine
                ],
            )
        if batch.findings:
            cursor.executemany(
                _FINDING_INSERT,
                [
                    (
                        ctx.tenant_id,
                        ctx.batch_id,
                        spec.key,
                        ctx.source_file_id,
                        f.key_values,
                        f.line_numbers,
                        f.identical,
                        ctx.created_at,
                    )  # fmt: skip
                    for f in batch.findings
                ],
            )
        sql = supersede_sql(spec)
        for s in batch.supersessions:
            batch_id, file_id, line = s.ref
            cursor.execute(
                sql,
                ctx.batch_id, ctx.created_at, s.reason, ctx.created_at, ctx.created_by,
                batch_id, file_id, line,
            )  # fmt: skip
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"curated row {s.ref} is not live (rowcount {cursor.rowcount}); "
                    "cannot supersede"
                )


def describe_statements(spec: SourceSpec) -> str:
    """The INSERT and UPDATE text for one source, for review. No values."""
    return json.dumps(
        {
            "landed": insert_sql(table_name(spec, "landed"), landed_columns(spec)),
            "typed": insert_sql(table_name(spec, "typed"), typed_columns(spec)),
            "curated": insert_sql(table_name(spec, "curated"), curated_columns(spec)),
            "supersede": supersede_sql(spec),
        },
        indent=2,
    )
