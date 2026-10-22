"""An in-memory store: the same tables as Azure SQL, in dictionaries.

Exists so the commit engine can be tested end to end without a server, and
so ``scripts/commit_file.py --dry-run`` can show exactly what a load would
write. Every rule the SQL store relies on the database for is reproduced
here in plain Python: a transaction is a snapshot restored on failure, the
duplicate-file check is a scan of ``source_files``, ``current_rows`` is the
same predicate the SQL query uses, and a supersession refuses to retire a
row that is not live.

Rows are dictionaries keyed exactly as the generated tables name their
columns (``store.schema``), so a test can read them like a query result.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from typing import Any

from sbh_data_bank.commit.records import (
    BatchRecord,
    CurrentQuery,
    FoundFile,
    RunRecord,
    SourceFileRecord,
    StoredCurrent,
    WriteBatch,
    WriteContext,
)
from sbh_data_bank.curate.rule import CurrentRow
from sbh_data_bank.registry.spec import SourceSpec
from sbh_data_bank.store.schema import DEDUP_RULE

__all__ = ["MemoryStore"]


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(0)


class MemoryStore:
    """Dictionaries shaped like the warehouse. Implements ``records.Store``."""

    def __init__(self) -> None:
        self.runs: dict[str, RunRecord] = {}
        self.source_files: dict[int, SourceFileRecord] = {}
        self.batches: dict[str, BatchRecord] = {}
        self.landed: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.typed: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.curated: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.quarantine: list[dict[str, Any]] = []
        self.findings: list[dict[str, Any]] = []
        self._next_file_id = 1

    # ----------------------------------------------------------- transactions

    def _state(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "source_files": self.source_files,
            "batches": self.batches,
            "landed": self.landed,
            "typed": self.typed,
            "curated": self.curated,
            "quarantine": self.quarantine,
            "findings": self.findings,
            "_next_file_id": self._next_file_id,
        }

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Snapshot on entry, restore on any exception: what rollback means."""
        saved = copy.deepcopy(self._state())
        try:
            yield
        except BaseException:
            for name, value in saved.items():
                setattr(self, name, value)
            raise

    # -------------------------------------------------------------- meta rows

    def insert_run(self, run: RunRecord) -> None:
        self.runs[run.run_id] = copy.copy(run)

    def finish_run(self, run: RunRecord) -> None:
        self.runs[run.run_id] = copy.copy(run)

    def find_source_file(
        self, tenant_id: str, source_key: str, file_sha256: str, sheet_name: str | None
    ) -> FoundFile | None:
        for file_id, record in self.source_files.items():
            if (
                record.tenant_id == tenant_id
                and record.source_key == source_key
                and record.file_sha256 == file_sha256
                and record.sheet_name == sheet_name
            ):
                attempts = [b for b in self.batches.values() if b.source_file_id == file_id]
                committed = any(b.outcome == "LOADED" for b in attempts)
                return FoundFile(file_id, committed, len(attempts))
        return None

    def insert_source_file(self, record: SourceFileRecord) -> int:
        file_id = self._next_file_id
        self._next_file_id += 1
        stored = copy.copy(record)
        stored.source_file_id = file_id
        self.source_files[file_id] = stored
        return file_id

    def delivery_rank(self, tenant_id: str, source_key: str, as_of: date) -> int:
        earlier = sum(
            1
            for r in self.source_files.values()
            if r.tenant_id == tenant_id
            and r.source_key == source_key
            and r.as_of is not None
            and r.as_of < as_of
        )
        return earlier + 1

    def insert_batch(self, batch: BatchRecord) -> None:
        if batch.batch_id in self.batches:
            raise ValueError(f"batch {batch.batch_id} already exists")
        self.batches[batch.batch_id] = copy.copy(batch)

    def finish_batch(self, batch: BatchRecord) -> None:
        if batch.batch_id not in self.batches:
            raise ValueError(f"batch {batch.batch_id} was never inserted")
        self.batches[batch.batch_id] = copy.copy(batch)

    # ----------------------------------------------------------- curated reads

    def current_rows(
        self, spec: SourceSpec, tenant_id: str, query: CurrentQuery
    ) -> list[StoredCurrent]:
        """Live rows sharing an incoming identity, plus live rows inside the
        delivery's coverage window: the same predicate as the SQL store."""
        found = []
        for row in self.curated[spec.key]:
            if row["tenant_id"] != tenant_id or row["superseded_by"] is not None:
                continue
            key = (row["identity_hash"], row["occurrence"])
            in_window = False
            if query.coverage is not None and query.coverage_column is not None:
                day = row.get(query.coverage_column)
                in_window = isinstance(day, date) and query.coverage.covers(day)
            if key not in query.keys and not in_window:
                continue
            event_date = row["as_of"] if query.snapshot_as_of_is_event_date else None
            if query.event_column is not None and not query.snapshot_as_of_is_event_date:
                event_date = row.get(query.event_column)
            found.append(
                StoredCurrent(
                    key[0],
                    key[1],
                    CurrentRow(
                        row_hash=row["row_hash"],
                        as_of=row["as_of"],
                        delivery=row["batch_id"],
                        line_number=row["line_number"],
                        event_date=event_date,
                        scope=tuple(row.get(c) for c in query.scope_columns),
                        controls=tuple(_decimal(row.get(c)) for c in query.control_columns),
                        ref=(row["batch_id"], row["source_file_id"], row["line_number"]),
                    ),
                )
            )
        return found

    # ---------------------------------------------------------------- writes

    def write(self, spec: SourceSpec, ctx: WriteContext, batch: WriteBatch) -> None:
        audit = {
            "tenant_id": ctx.tenant_id,
            "source_file_id": ctx.source_file_id,
            "created_at": ctx.created_at,
            "created_by": ctx.created_by,
            "updated_at": None,
            "updated_by": None,
        }
        for landed in batch.landed:
            self.landed[spec.key].append(
                {
                    **audit,
                    "line_number": landed.line_number,
                    **{t: landed.cells.get(t) for t in spec.landed_targets},
                    "extra_cells": landed.extra_cells,
                }
            )
        for typed in batch.typed:
            self.typed[spec.key].append(
                {
                    **audit,
                    "line_number": typed.line_number,
                    "spec_sha256": typed.spec_sha256,
                    "row_hash": typed.row_hash,
                    **{t: typed.values.get(t) for t in spec.landed_targets},
                }
            )
        for curated in batch.curated:
            self.curated[spec.key].append(
                {
                    **audit,
                    "batch_id": ctx.batch_id,
                    "line_number": curated.line_number,
                    "dedup_rule": DEDUP_RULE,
                    "identity_hash": curated.identity_hash,
                    "occurrence": curated.occurrence,
                    "row_hash": curated.row_hash,
                    "as_of": curated.as_of,
                    "outcome": curated.outcome,
                    "superseded_by": curated.superseded_by,
                    "superseded_at": curated.superseded_at,
                    "supersede_reason": curated.supersede_reason,
                    "updated_at": ctx.created_at if curated.superseded_by else None,
                    "updated_by": ctx.created_by if curated.superseded_by else None,
                    **{t: curated.values.get(t) for t in spec.landed_targets},
                }
            )
        for q in batch.quarantine:
            self.quarantine.append(
                {
                    "tenant_id": ctx.tenant_id,
                    "batch_id": ctx.batch_id,
                    "source_key": spec.key,
                    "source_file_id": ctx.source_file_id,
                    "line_number": q.line_number,
                    "reasons": q.reasons,
                    "errors": q.errors,
                    "payload": q.payload,
                    "detected_at": ctx.created_at,
                    "resolved_at": None,
                    "resolved_by": None,
                    "resolution": None,
                }
            )
        for f in batch.findings:
            self.findings.append(
                {
                    "tenant_id": ctx.tenant_id,
                    "batch_id": ctx.batch_id,
                    "source_key": spec.key,
                    "source_file_id": ctx.source_file_id,
                    "key_values": f.key_values,
                    "line_numbers": f.line_numbers,
                    "identical": f.identical,
                    "detected_at": ctx.created_at,
                }
            )
        by_ref = {
            (r["batch_id"], r["source_file_id"], r["line_number"]): r
            for r in self.curated[spec.key]
        }
        for s in batch.supersessions:
            row = by_ref.get(s.ref)
            if row is None or row["superseded_by"] is not None:
                # The row moved under us (another load, or a bug). The SQL
                # store sees rowcount 0 here; both refuse rather than lose
                # a supersession silently.
                raise RuntimeError(f"curated row {s.ref} is not live; cannot supersede")
            row["superseded_by"] = ctx.batch_id
            row["superseded_at"] = ctx.created_at
            row["supersede_reason"] = s.reason
            row["updated_at"] = ctx.created_at
            row["updated_by"] = ctx.created_by

    # ------------------------------------------------------------ for tests

    def live(self, source_key: str) -> list[dict[str, Any]]:
        """Current curated rows of one source, in insertion order."""
        return [r for r in self.curated[source_key] if r["superseded_by"] is None]

    def snapshot(self) -> dict[str, Any]:
        """Everything, deep-copied, for "replaying twice gives the same
        state" comparisons."""
        return copy.deepcopy(self._state())
