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
import json
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from typing import Any

from sbh_data_bank.commit.rebuild import RETYPED, ColumnRetype, RebuildCounts, TargetRow
from sbh_data_bank.commit.records import (
    BatchRecord,
    CurrentQuery,
    FindingWrite,
    FoundFile,
    LoadedFile,
    RunRecord,
    SourceFileRecord,
    StoredCurrent,
    TypedWrite,
    WriteBatch,
    WriteContext,
)
from sbh_data_bank.curate.rule import CurrentRow
from sbh_data_bank.ingest.replay import LandedLine
from sbh_data_bank.registry.spec import SourceSpec
from sbh_data_bank.store.schema import DEDUP_RULE, typed_sql_types

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
        #: The SQL type each business column of a source was created with,
        #: recorded at the source's first write as the migration would have
        #: made the table, so a spec that later types a column differently
        #: shows as a retype here exactly as it does against the catalog.
        self.column_types: dict[str, dict[str, str]] = {}
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
            "column_types": self.column_types,
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
            if query.coverage is not None and query.snapshot_as_of_is_event_date:
                # A snapshot row's date is its delivery's as-of (2026-11-05:
                # the pricing list), so the window is on as_of.
                in_window = query.coverage.covers(row["as_of"])
            elif query.coverage is not None and query.coverage_column is not None:
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
        self.column_types.setdefault(
            spec.key, dict(zip(spec.landed_targets, typed_sql_types(spec), strict=True))
        )
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

    # ---------------------------------------------------------- reading back

    def loaded_files(self, tenant_id: str, source_key: str) -> list[LoadedFile]:
        found = []
        for file_id, record in sorted(self.source_files.items()):
            if record.tenant_id != tenant_id or record.source_key != source_key:
                continue
            loaded = [
                b
                for b in self.batches.values()
                if b.source_file_id == file_id and b.outcome == "LOADED"
            ]
            if not loaded or record.header_json is None:
                continue
            (batch,) = loaded
            found.append(
                LoadedFile(
                    file_id,
                    record.file_name,
                    record.sheet_name,
                    record.header_json,
                    batch.batch_id,
                    batch.control_totals,
                    record.as_of,
                    record.coverage_from,
                    record.coverage_to,
                )
            )
        return found

    # ------------------------------------------------------------- rebuild

    def replace_typed(
        self, spec: SourceSpec, ctx: WriteContext, source_file_id: int, rows: list[TypedWrite]
    ) -> None:
        kept = [r for r in self.typed[spec.key] if r["source_file_id"] != source_file_id]
        for typed in rows:
            kept.append(
                {
                    "tenant_id": ctx.tenant_id,
                    "source_file_id": source_file_id,
                    "line_number": typed.line_number,
                    "spec_sha256": typed.spec_sha256,
                    "row_hash": typed.row_hash,
                    "created_at": ctx.created_at,
                    "created_by": ctx.created_by,
                    "updated_at": None,
                    "updated_by": None,
                    **{t: typed.values.get(t) for t in spec.landed_targets},
                }
            )
        self.typed[spec.key] = kept

    def replace_findings(
        self, spec: SourceSpec, ctx: WriteContext, source_file_id: int, findings: list[FindingWrite]
    ) -> None:
        self.findings = [
            f
            for f in self.findings
            if not (f["source_key"] == spec.key and f["source_file_id"] == source_file_id)
        ]
        for f in findings:
            self.findings.append(
                {
                    "tenant_id": ctx.tenant_id,
                    "batch_id": ctx.batch_id,
                    "source_key": spec.key,
                    "source_file_id": source_file_id,
                    "key_values": f.key_values,
                    "line_numbers": f.line_numbers,
                    "identical": f.identical,
                    "detected_at": ctx.created_at,
                }
            )

    def reconcile_curated(
        self, spec: SourceSpec, ctx: WriteContext, target: list[TargetRow], *, apply: bool
    ) -> RebuildCounts:
        """The SQL store's two set operations, as dictionary scans: a live
        row is kept when a target row names its line with the same identity,
        occurrence and content."""
        by_line = {(t.source_file_id, t.line_number): t for t in target}

        def matched(row: dict[str, Any]) -> bool:
            t = by_line.get((row["source_file_id"], row["line_number"]))
            return (
                t is not None
                and t.identity_hash == row["identity_hash"]
                and t.occurrence == row["occurrence"]
                and t.row_hash == row["row_hash"]
            )

        live = [
            r
            for r in self.curated[spec.key]
            if r["tenant_id"] == ctx.tenant_id and r["superseded_by"] is None
        ]
        retire = [r for r in live if not matched(r)]
        kept_lines = {(r["source_file_id"], r["line_number"]) for r in live if matched(r)}
        insert = [t for t in target if (t.source_file_id, t.line_number) not in kept_lines]
        counts = RebuildCounts(len(target) - len(insert), len(retire), len(insert))
        if not apply:
            return counts
        for row in retire:
            row["superseded_by"] = ctx.batch_id
            row["superseded_at"] = ctx.created_at
            row["supersede_reason"] = RETYPED
            row["updated_at"] = ctx.created_at
            row["updated_by"] = ctx.created_by
        typed_by_line = {
            (r["source_file_id"], r["line_number"]): r
            for r in self.typed[spec.key]
            if r["tenant_id"] == ctx.tenant_id
        }
        for t in insert:
            typed = typed_by_line[(t.source_file_id, t.line_number)]
            self.curated[spec.key].append(
                {
                    "tenant_id": ctx.tenant_id,
                    "batch_id": ctx.batch_id,
                    "source_file_id": t.source_file_id,
                    "line_number": t.line_number,
                    "dedup_rule": DEDUP_RULE,
                    "identity_hash": t.identity_hash,
                    "occurrence": t.occurrence,
                    "row_hash": t.row_hash,
                    "as_of": t.as_of,
                    "outcome": t.outcome,
                    "created_at": ctx.created_at,
                    "created_by": ctx.created_by,
                    "superseded_by": None,
                    "superseded_at": None,
                    "supersede_reason": None,
                    "updated_at": None,
                    "updated_by": None,
                    **{c: typed.get(c) for c in spec.landed_targets},
                }
            )
        return counts

    def column_retypes(self, spec: SourceSpec) -> list[ColumnRetype]:
        live = self.column_types.get(spec.key, {})
        return [
            ColumnRetype(target, live[target], spec_type)
            for target, spec_type in zip(spec.landed_targets, typed_sql_types(spec), strict=True)
            if target in live and live[target].upper() != spec_type.upper()
        ]

    def retype_columns(
        self, spec: SourceSpec, ctx: WriteContext, retypes: list[ColumnRetype]
    ) -> None:
        for layer in (self.typed, self.curated):
            for row in layer[spec.key]:
                if row["tenant_id"] == ctx.tenant_id:
                    for retype in retypes:
                        row[retype.target] = None
        self.column_types[spec.key].update({r.target: r.spec_type for r in retypes})

    def refill_curated(
        self, spec: SourceSpec, ctx: WriteContext, retypes: list[ColumnRetype], *, apply: bool
    ) -> tuple[int, int]:
        typed_by_line = {
            (r["source_file_id"], r["line_number"]): r
            for r in self.typed[spec.key]
            if r["tenant_id"] == ctx.tenant_id
        }
        rows = [r for r in self.curated[spec.key] if r["tenant_id"] == ctx.tenant_id]
        orphans = sum(
            1 for r in rows if (r["source_file_id"], r["line_number"]) not in typed_by_line
        )
        matched = len(rows) - orphans
        if orphans or not apply:
            return matched, orphans
        for row in rows:
            typed = typed_by_line[(row["source_file_id"], row["line_number"])]
            for retype in retypes:
                row[retype.target] = typed.get(retype.target)
        return matched, orphans

    def _history_rows(self, spec: SourceSpec, tenant_id: str) -> list[dict[str, Any]]:
        return [
            r
            for r in self.curated[spec.key]
            if r["tenant_id"] == tenant_id
            and r.get("superseded_by") is not None
            and r.get("supersede_reason") != RETYPED
        ]

    def history_hashes(self, spec: SourceSpec, tenant_id: str) -> dict[tuple[int, int], str]:
        return {
            (r["source_file_id"], r["line_number"]): r["row_hash"]
            for r in self._history_rows(spec, tenant_id)
        }

    def refresh_hashes(self, spec: SourceSpec, ctx: WriteContext) -> int:
        typed_hash = {
            (r["source_file_id"], r["line_number"]): r["row_hash"]
            for r in self.typed[spec.key]
            if r["tenant_id"] == ctx.tenant_id
        }
        written = 0
        for row in self._history_rows(spec, ctx.tenant_id):
            new = typed_hash.get((row["source_file_id"], row["line_number"]))
            if new is not None and row["row_hash"] != new:
                row["row_hash"] = new
                written += 1
        return written

    def landed_lines(self, spec: SourceSpec, source_file_id: int) -> Iterator[LandedLine]:
        rows = [r for r in self.landed[spec.key] if r["source_file_id"] == source_file_id]
        for row in sorted(rows, key=lambda r: r["line_number"]):
            yield LandedLine(
                row["line_number"],
                {t: row.get(t) for t in spec.landed_targets},
                json.loads(row["extra_cells"]) if row["extra_cells"] else {},
            )

    def typed_hashes(self, spec: SourceSpec, source_file_id: int) -> dict[int, str]:
        return {
            r["line_number"]: r["row_hash"]
            for r in self.typed[spec.key]
            if r["source_file_id"] == source_file_id
        }

    def quarantined_lines(self, batch_id: str) -> dict[int, str]:
        return {
            q["line_number"]: q["reasons"] for q in self.quarantine if q["batch_id"] == batch_id
        }

    # ------------------------------------------------------------ for tests

    def live(self, source_key: str) -> list[dict[str, Any]]:
        """Current curated rows of one source, in insertion order."""
        return [r for r in self.curated[source_key] if r["superseded_by"] is None]

    def snapshot(self) -> dict[str, Any]:
        """Everything, deep-copied, for "replaying twice gives the same
        state" comparisons."""
        return copy.deepcopy(self._state())
