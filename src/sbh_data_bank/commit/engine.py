"""The commit engine: one file's load, written through the layers.

Takes a :class:`~sbh_data_bank.ingest.load.FileLoad` and persists it: the
file in ``meta.source_file``, the attempt in ``meta.batch``, every line in
``landed.*``, every typed row in ``typed.*``, every quarantined row in
``stg.quarantine``, and what the dedup rule decides in ``curated.*`` - in
one transaction, so a load either happened or did not. The rule itself is
``curate.rule.Ledger``; this module seeds a ledger with the live rows the
delivery touches, lets it decide, and writes exactly what it decided.

What can stop a load, in the order it is checked, and what is written then:

===========================  ===================================================
``DUPLICATE_FILE``           the content hash already has a committed batch;
                             the attempt is recorded, nothing else
the load's own outcome       ``UNRECOGNISED``, ``BLOCKED_BY_DRIFT``,
                             ``CONTROL_TOTAL_MISMATCH``, ... (``LoadOutcome``):
                             the file is registered, the batch says why
``AS_OF_UNAVAILABLE``        no dated folder, stamp, filename date or operator
                             date (D5): refused, not guessed
``HELD_AS_OF_FLOOR``         a row is dated after the as-of (D4)
``HELD_COVERAGE_UNKNOWN``    the spec vanishes rows but the delivery's window
                             cannot be known, so the rule could not run
                             honestly (engine decision, 2026-10-20)
``HELD_FILENAME_WINDOW``     the rows contradict the window the filename
                             claims (D12)
``HELD_CONFLICT``            same as-of, different content for one identity:
                             a person decides, the whole batch waits
``LOADED``                   everything written, counts on the batch
===========================  ===================================================

A held or refused batch persists its ``meta.source_file`` and ``meta.batch``
rows and nothing else, so the attempt is on the record and a corrected
re-upload is not mistaken for a duplicate (only a LOADED batch makes a hash
a duplicate). A late delivery is not held: its rows land in every layer and
enter ``curated`` already superseded, as history.

How this fits
-------------
``scripts/commit_file.py`` is the caller today. The engine talks to a
:class:`~sbh_data_bank.commit.records.Store`: ``commit.sql.SqlStore`` for
Azure SQL, ``commit.memory.MemoryStore`` for tests and dry runs. Ids come
from ``core.identity``; ``deterministic_ids=True`` derives the batch id from
the file hash so a replay of the same files is byte-identical (definition
of done, "Behaviour under replay").
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sbh_data_bank.commit.records import (
    BatchRecord,
    CuratedWrite,
    CurrentQuery,
    FindingWrite,
    LandedWrite,
    QuarantineWrite,
    RunRecord,
    SourceFileRecord,
    Store,
    Supersession,
    TypedWrite,
    WriteBatch,
    WriteContext,
)
from sbh_data_bank.core.identity import (
    deterministic_batch_id,
    file_sha256,
    new_batch_id,
    new_run_id,
)
from sbh_data_bank.curate.delivery import (
    AS_OF_TOLERANCE_DAYS,
    AsOf,
    AsOfUnavailableError,
    Coverage,
    as_of_floor_violation,
    resolve_as_of,
    resolve_coverage,
)
from sbh_data_bank.curate.rule import (
    CurrentRow,
    DeliveryFacts,
    Ledger,
    Outcomes,
    RowFact,
    facts_for,
)
from sbh_data_bank.ingest.load import FileLoad, LoadOutcome, load_file, load_workbook
from sbh_data_bank.registry.spec import CoverageSource, SourceSpec
from sbh_data_bank.store.schema import DEDUP_RULE

__all__ = [
    "WINDOW_SLACK_DAYS",
    "CommitEngine",
    "CommitOutcome",
    "CommitResult",
    "filename_window_check",
    "spec_sha256",
    "utc_now",
]


class CommitOutcome:
    """Batch outcomes the engine adds to ``ingest.load.LoadOutcome``. Strings,
    stored on ``meta.batch.outcome``. Only ``LOADED`` means rows were written."""

    LOADED = LoadOutcome.LOADED
    DUPLICATE_FILE = "DUPLICATE_FILE"
    AS_OF_UNAVAILABLE = "AS_OF_UNAVAILABLE"
    HELD_CONFLICT = "HELD_CONFLICT"
    HELD_AS_OF_FLOOR = "HELD_AS_OF_FLOOR"
    HELD_FILENAME_WINDOW = "HELD_FILENAME_WINDOW"
    HELD_COVERAGE_UNKNOWN = "HELD_COVERAGE_UNKNOWN"


#: D12: a filename window wider than the data by more than this, at either
#: end, is a warning on the batch. A quiet first week in a window is normal;
#: a quiet first quarter is a sign the name overstates what was pulled. Not
#: yet a per-source setting; the decision record says it should become one.
WINDOW_SLACK_DAYS = 31

_WORKBOOK_SUFFIXES = {".xlsx", ".xlsm", ".xls"}


def utc_now() -> datetime:
    """Transaction time, UTC, naive: ``DATETIME2`` carries no zone and the
    ODBC driver refuses an aware value."""
    return datetime.now(UTC).replace(tzinfo=None)


def spec_sha256(spec: SourceSpec) -> str:
    """The typing rules' identity for ``typed.*.spec_sha256``: a digest of the
    validated spec, not of the YAML file, so a comment edit does not look like
    a rule change and a rule change cannot hide behind unchanged bytes."""
    return hashlib.sha256(spec.model_dump_json(exclude_none=True).encode()).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


@dataclass(slots=True)
class CommitResult:
    """What one commit attempt did, for the caller and the log."""

    batch_id: str
    outcome: str
    detail: str
    source_file_id: int | None
    filename: str
    outcomes: Outcomes | None = None
    """The rule's counters when it ran (LOADED or HELD_CONFLICT)."""
    warnings: list[str] = field(default_factory=list)

    @property
    def committed(self) -> bool:
        return self.outcome == CommitOutcome.LOADED

    def summary(self) -> str:
        """One line: file, outcome, the counters when there are any, detail."""
        parts = [self.filename, f"[{self.outcome}]"]
        if self.outcomes is not None:
            o = self.outcomes
            parts.append(
                f"inserted={o.inserted} unchanged={o.unchanged} corrected={o.corrected} "
                f"late={o.late} conflict={o.conflict} vanished={o.vanished}"
            )
        if self.warnings:
            parts.append("warnings: " + "; ".join(self.warnings))
        if self.detail:
            parts.append(f"- {self.detail}")
        return " ".join(parts)


def filename_window_check(
    spec: SourceSpec,
    coverage: Coverage | None,
    as_of: date,
    coverage_dates: Iterable[Any],
) -> tuple[str | None, list[str]]:
    """Decision D12: the data must confirm a filename's claimed window.

    Applies only when the spec takes coverage from the filename. Returns
    ``(hold_reason, warnings)``: a hold when the claimed end is later than
    the as-of (beyond the D4 tolerance) or any row is dated before the
    claimed start; warnings when the window is wider than the data by more
    than :data:`WINDOW_SLACK_DAYS` at either end.
    """
    if spec.coverage is not CoverageSource.FILENAME or coverage is None:
        return None, []
    days = sorted(d for d in coverage_dates if isinstance(d, date))
    column = spec.coverage_target
    if coverage.end > as_of + timedelta(days=AS_OF_TOLERANCE_DAYS):
        return (
            f"filename_window_disagrees: the name claims coverage to {coverage.end} "
            f"but the delivery's as-of is {as_of}",
            [],
        )
    if coverage.start is not None and days and days[0] < coverage.start:
        early = sum(1 for d in days if d < coverage.start)
        return (
            f"filename_window_disagrees: {early} rows have {column} before the claimed "
            f"start {coverage.start} (earliest {days[0]})",
            [],
        )
    warnings = []
    if days:
        if coverage.start is not None and (days[0] - coverage.start).days > WINDOW_SLACK_DAYS:
            warnings.append(
                f"claimed window starts {coverage.start} but the earliest {column} is {days[0]}"
            )
        if (coverage.end - days[-1]).days > WINDOW_SLACK_DAYS:
            warnings.append(
                f"claimed window ends {coverage.end} but the latest {column} is {days[-1]}"
            )
    return None, warnings


class CommitEngine:
    """Writes loads through a :class:`Store`, one file per transaction."""

    def __init__(
        self,
        store: Store,
        *,
        tenant_id: str,
        actor: str,
        clock: Callable[[], datetime] = utc_now,
        deterministic_ids: bool = False,
    ) -> None:
        """``actor`` is stamped as ``created_by`` on every row; ``clock``
        supplies transaction time (tests pass a fixed one);
        ``deterministic_ids`` derives batch ids from content, for replays."""
        self._store = store
        self.tenant_id = tenant_id
        self.actor = actor
        self._clock = clock
        self._deterministic = deterministic_ids

    # ------------------------------------------------------------------ runs

    def begin_run(self, trigger_kind: str, run_id: str | None = None) -> RunRecord:
        """Open a ``meta.run`` row grouping the batches that follow."""
        run = RunRecord(
            run_id=run_id or new_run_id(),
            tenant_id=self.tenant_id,
            trigger_kind=trigger_kind,
            started_at=self._clock(),
        )
        with self._store.transaction():
            self._store.insert_run(run)
        return run

    def finish_run(self, run: RunRecord, outcome: str, detail: str | None = None) -> None:
        run.finished_at = self._clock()
        run.outcome = outcome
        run.detail = detail
        with self._store.transaction():
            self._store.finish_run(run)

    # ----------------------------------------------------------------- files

    def commit_path(
        self,
        spec: SourceSpec,
        path: Path,
        *,
        run_id: str,
        root: Path | None = None,
        operator_date: date | None = None,
        delivery: str | None = None,
    ) -> list[CommitResult]:
        """Load one file from disk and commit it: one result per CSV, or per
        worksheet of a workbook.

        The as-of is resolved by the D5 chain (``root`` bounds the folder
        search); a file with none is recorded as ``AS_OF_UNAVAILABLE``.
        ``delivery`` labels the drop or upload the file arrived in; it
        defaults to the file's folder path below ``root`` when ``root`` is
        given and the file is not directly under it.
        """
        as_of: AsOf | None = None
        problem = ""
        try:
            as_of = resolve_as_of(path, spec, operator_date=operator_date, root=root)
        except AsOfUnavailableError as exc:
            problem = str(exc)
        if delivery is None and root is not None and path.parent != root:
            delivery = path.parent.relative_to(root).as_posix()
        if path.suffix.lower() in _WORKBOOK_SUFFIXES:
            loads = load_workbook(path, spec)
        else:
            loads = [load_file(path, spec)]
        size = path.stat().st_size
        for load in loads:
            if load.file_hash is None:
                # An unrecognised file is not hashed by the loader; it is
                # still registered, so the attempt is on the record.
                load.file_hash = file_sha256(path)
        return [
            self.commit_load(
                spec,
                load,
                run_id=run_id,
                as_of=as_of,
                as_of_problem=problem,
                delivery=delivery,
                file_bytes=size,
            )
            for load in loads
        ]

    def commit_load(
        self,
        spec: SourceSpec,
        load: FileLoad,
        *,
        run_id: str,
        as_of: AsOf | None,
        as_of_problem: str = "",
        delivery: str | None = None,
        file_bytes: int | None = None,
    ) -> CommitResult:
        """Commit one load. Always returns a result; raises only on a store
        failure, in which case nothing of this load was written."""
        if load.file_hash is None:
            raise ValueError(f"{load.filename}: a load needs a file hash to commit")
        now = self._clock()
        batch = BatchRecord(
            batch_id="",
            tenant_id=self.tenant_id,
            run_id=run_id,
            source_key=spec.key,
            started_at=now,
            variant_name=load.variant_name,
            dedup_mode=DEDUP_RULE,
            rows_landed=len(load.landed),
            rows_read=load.counters.rows_read,
            rows_quarantined=load.counters.rows_quarantined,
        )
        coverage_column = spec.coverage_target
        coverage_dates = (
            [r.values.get(coverage_column) for r in load.rows] if coverage_column else []
        )
        coverage = None
        if as_of is not None and load.ok:
            coverage = resolve_coverage(
                spec, filename=load.filename, as_of=as_of.value, coverage_dates=coverage_dates
            )

        with self._store.transaction():
            found = self._store.find_source_file(self.tenant_id, load.file_hash, load.sheet)
            batch.batch_id = self._batch_id(spec, load, found.attempts if found else 0)
            if found is not None and found.committed:
                # Already committed under this content hash: record the
                # attempt against the existing file and write nothing else.
                batch.source_file_id = found.source_file_id
                batch.outcome = CommitOutcome.DUPLICATE_FILE
                batch.detail = (
                    f"content hash already committed as source_file {found.source_file_id}"
                )
                batch.finished_at = self._clock()
                self._store.insert_batch(batch)
                return self._result(batch, load)
            if found is not None:
                file_id = found.source_file_id
            else:
                file_id = self._register_file(
                    spec, load, as_of, coverage, now, delivery=delivery, file_bytes=file_bytes
                )
            batch.source_file_id = file_id
            self._store.insert_batch(batch)

            refusal = self._refusal(spec, load, as_of, as_of_problem, coverage, coverage_dates)
            if refusal is not None:
                return self._finish(batch, load, *refusal)
            assert as_of is not None
            _hold, warnings = filename_window_check(spec, coverage, as_of.value, coverage_dates)
            return self._apply_and_write(spec, load, batch, as_of, coverage, now, warnings)

    # -------------------------------------------------------------- the steps

    def _batch_id(self, spec: SourceSpec, load: FileLoad, attempts: int) -> str:
        """A fresh id, or in replay mode one derived from the content (and
        the attempt number, so a re-attempt of the same file is distinct)."""
        if self._deterministic:
            suffix = f":attempt{attempts}" if attempts else ""
            return deterministic_batch_id(f"{load.file_hash}:{load.sheet or ''}{suffix}", spec.key)
        return new_batch_id()

    def _register_file(
        self,
        spec: SourceSpec,
        load: FileLoad,
        as_of: AsOf | None,
        coverage: Coverage | None,
        now: datetime,
        *,
        delivery: str | None,
        file_bytes: int | None,
    ) -> int:
        header_json = None
        if load.binding is not None:
            targets = {
                cb.match.position: cb.column.target
                for cb in load.binding.columns
                if cb.match is not None
            }
            header_json = _json(
                [
                    {"position": i, "header": h, "target": targets.get(i)}
                    for i, h in enumerate(load.header)
                ]
            )
        assert load.file_hash is not None
        record = SourceFileRecord(
            tenant_id=self.tenant_id,
            source_key=spec.key,
            file_name=load.filename,
            file_sha256=load.file_hash,
            first_seen_at=now,
            file_bytes=file_bytes,
            sheet_name=load.sheet,
            delivery=delivery,
            delivery_rank=(
                self._store.delivery_rank(self.tenant_id, spec.key, as_of.value) if as_of else 0
            ),
            as_of=as_of.value if as_of else None,
            as_of_source=as_of.source.value if as_of else None,
            coverage_from=coverage.start if coverage else None,
            coverage_to=coverage.end if coverage else None,
            header_json=header_json,
        )
        return self._store.insert_source_file(record)

    def _refusal(
        self,
        spec: SourceSpec,
        load: FileLoad,
        as_of: AsOf | None,
        as_of_problem: str,
        coverage: Coverage | None,
        coverage_dates: list[Any],
    ) -> tuple[str, str] | None:
        """The first reason this load cannot commit, as (outcome, detail),
        or ``None`` when the rule may run."""
        if not load.ok:
            return load.outcome, load.detail
        if as_of is None:
            return CommitOutcome.AS_OF_UNAVAILABLE, as_of_problem or "no as-of date"
        event_column = spec.event_date_column
        if event_column is not None:
            problem = as_of_floor_violation(
                as_of.value, (r.values.get(event_column) for r in load.rows)
            )
            if problem:
                return CommitOutcome.HELD_AS_OF_FLOOR, problem
        if coverage is None and spec.vanishes:
            return (
                CommitOutcome.HELD_COVERAGE_UNKNOWN,
                f"{spec.key} retires rows a covering delivery no longer carries, but this "
                f"delivery's window cannot be known (coverage from {spec.coverage.value}); "
                "name the window in the filename or correct the spec",
            )
        hold, _warnings = filename_window_check(spec, coverage, as_of.value, coverage_dates)
        if hold:
            return CommitOutcome.HELD_FILENAME_WINDOW, hold
        return None

    def _apply_and_write(
        self,
        spec: SourceSpec,
        load: FileLoad,
        batch: BatchRecord,
        as_of: AsOf,
        coverage: Coverage | None,
        now: datetime,
        warnings: list[str],
    ) -> CommitResult:
        """Seed a ledger with the live rows this delivery touches, let the
        rule decide, and write what it decided."""
        controls = sorted(load.counters.control_totals)
        facts = facts_for(spec, load.rows, as_of=as_of.value, controls=controls)
        query = CurrentQuery(
            keys=frozenset((f.identity_hash, f.occurrence) for f in facts),
            coverage=coverage if spec.vanishes else None,
            coverage_column=spec.coverage_target,
            event_column=spec.event_date_column,
            scope_columns=tuple(spec.vanish_scope),
            control_columns=tuple(controls),
            snapshot_as_of_is_event_date=spec.is_snapshot,
        )
        ledger = Ledger.for_spec(spec)
        for stored in self._store.current_rows(spec, self.tenant_id, query):
            ledger.current[stored.key] = stored.row
        before = dict(ledger.current)

        delivery = DeliveryFacts(batch.batch_id, as_of.value, coverage, facts)
        decided = ledger.decide(delivery)
        outcomes = ledger.apply(delivery)
        if outcomes.held:
            batch.rows_conflict = outcomes.conflict
            result = self._finish(
                batch,
                load,
                CommitOutcome.HELD_CONFLICT,
                f"{outcomes.conflict} rows carry the as-of {as_of.value} of a current row "
                "with different content; a person decides which is right",
                warnings=warnings,
            )
            result.outcomes = outcomes
            return result

        writes = self._writes(spec, load, batch, as_of.value, decided, before, ledger, now)
        batch.rows_inserted = outcomes.inserted
        batch.rows_superseded = outcomes.corrected
        batch.rows_unchanged = outcomes.unchanged
        batch.rows_late = outcomes.late
        batch.rows_vanished = outcomes.vanished
        batch.control_totals = _json(
            {
                "sums": {k: str(v) for k, v in sorted(load.counters.control_totals.items())},
                "footer": None
                if load.footer is None
                else {
                    "line_number": load.footer.line_number,
                    "declared": {k: str(v) for k, v in sorted(load.footer.declared.items())},
                    "computed": {k: str(v) for k, v in sorted(load.footer.computed.items())},
                    "agrees": load.footer.agrees,
                },
                "warnings": warnings,
            }
        )
        if batch.accounted != batch.rows_read:
            # The meta.batch invariant. Unreachable unless the rule or the
            # loader miscounts; a batch that cannot account for its rows is a
            # bug, never a commit.
            raise RuntimeError(
                f"{load.filename}: batch accounts for {batch.accounted} rows of "
                f"{batch.rows_read} read; refusing to commit"
            )
        assert batch.source_file_id is not None
        ctx = WriteContext(
            tenant_id=self.tenant_id,
            batch_id=batch.batch_id,
            source_file_id=batch.source_file_id,
            created_by=self.actor,
            created_at=now,
        )
        self._store.write(spec, ctx, writes)
        result = self._finish(batch, load, CommitOutcome.LOADED, "", warnings=warnings)
        result.outcomes = outcomes
        return result

    def _writes(
        self,
        spec: SourceSpec,
        load: FileLoad,
        batch: BatchRecord,
        as_of: date,
        decided: list[tuple[RowFact, str]],
        before: dict[tuple[str, int], CurrentRow],
        ledger: Ledger,
        now: datetime,
    ) -> WriteBatch:
        """Turn the rule's decisions into rows for every layer."""
        digest = spec_sha256(spec)
        writes = WriteBatch()
        for landed in load.landed:
            writes.landed.append(
                LandedWrite(
                    landed.line_number,
                    landed.cells,
                    _json(landed.extra_cells) if landed.extra_cells else None,
                )
            )
        for row in load.rows:
            values = {k: v for k, v in row.values.items() if k != "_row_hash"}
            writes.typed.append(
                TypedWrite(row.line_number, digest, row.values["_row_hash"], values)
            )
        for (fact, outcome), row in zip(decided, load.rows, strict=True):
            if outcome == "unchanged":
                continue  # counted on the batch, nothing written to curated
            values = {k: v for k, v in row.values.items() if k != "_row_hash"}
            superseded_by = superseded_at = reason = None
            if outcome == "late":
                # History behind the row that beat it: superseded on arrival
                # by the batch that holds the current version.
                current = before[(fact.identity_hash, fact.occurrence)]
                superseded_by = current.ref[0] if current.ref else current.delivery
                superseded_at, reason = now, "late"
            writes.curated.append(
                CuratedWrite(
                    line_number=fact.line_number,
                    identity_hash=fact.identity_hash,
                    occurrence=fact.occurrence,
                    row_hash=fact.row_hash,
                    as_of=as_of,
                    outcome=outcome,
                    values=values,
                    superseded_by=superseded_by,
                    superseded_at=superseded_at,
                    supersede_reason=reason,
                )
            )
        for gone in ledger.history:
            if gone.by == batch.batch_id and gone.reason in ("corrected", "vanished"):
                writes.supersessions.append(Supersession(gone.row.ref, gone.reason))
        for q in load.quarantined:
            writes.quarantine.append(
                QuarantineWrite(
                    q.line_number,
                    ",".join(q.reasons),
                    _json(
                        [{"column": e.column, "reason": e.reason, "raw": e.raw} for e in q.errors]
                    ),
                    _json(q.payload),
                )
            )
        writes.findings = _findings(spec, decided, load)
        return writes

    def _finish(
        self,
        batch: BatchRecord,
        load: FileLoad,
        outcome: str,
        detail: str,
        *,
        warnings: list[str] | None = None,
    ) -> CommitResult:
        batch.outcome = outcome
        batch.detail = detail or None
        batch.finished_at = self._clock()
        self._store.finish_batch(batch)
        return self._result(batch, load, warnings)

    @staticmethod
    def _result(
        batch: BatchRecord, load: FileLoad, warnings: list[str] | None = None
    ) -> CommitResult:
        return CommitResult(
            batch_id=batch.batch_id,
            outcome=batch.outcome,
            detail=batch.detail or "",
            source_file_id=batch.source_file_id,
            filename=load.filename if load.sheet is None else f"{load.filename} [{load.sheet}]",
            warnings=list(warnings or []),
        )


def _findings(
    spec: SourceSpec, decided: list[tuple[RowFact, str]], load: FileLoad
) -> list[FindingWrite]:
    """One ``stg.duplicate_finding`` per identity that several lines share.

    ``key_values`` is the natural key's values as JSON, or the identity hash
    for a keyless spec (whose identity is every column and would not fit)."""
    groups: dict[str, list[tuple[RowFact, dict[str, Any]]]] = defaultdict(list)
    for (fact, _outcome), row in zip(decided, load.rows, strict=True):
        groups[fact.identity_hash].append((fact, row.values))
    findings = []
    for identity_hash, members in groups.items():
        if len(members) < 2:
            continue
        first_values = members[0][1]
        key_values = identity_hash
        if spec.natural_key:
            as_json = _json({k: first_values.get(k) for k in spec.natural_key})
            if len(as_json) <= 900:
                key_values = as_json
        findings.append(
            FindingWrite(
                key_values=key_values,
                line_numbers=",".join(str(f.line_number) for f, _ in members),
                identical=len({f.row_hash for f, _ in members}) == 1,
            )
        )
    return findings
