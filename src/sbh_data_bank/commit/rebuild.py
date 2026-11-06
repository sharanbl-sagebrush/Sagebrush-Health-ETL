"""Rebuild ``typed`` and ``curated`` from ``landed`` under the spec as it is
now: the write side of the replay, decision D18.

A rule change on a loaded source (a key column, a column type) is applied
here, inside the database, without reading a file again. For every file
the source has loaded, in as-of order, the landed rows are typed again
(``ingest.replay``) and the typed rows are replaced; the dedup rule then
runs over the whole source in memory, exactly as the loads ran it, to give
the state the rule would have produced had the spec always been this one.
That state is reconciled with the live curated rows:

=============  =========================================================
``kept``       a live row whose line is current under the new rule with
               the same identity hash, occurrence and row hash: untouched
``retired``    every other live row: superseded with reason ``retyped``,
               pointing at the rebuild batch, kept as history
``inserted``   every row current under the new rule that no kept row
               represents: written beside the retired one, with the
               business values the rebuilt typed row has
=============  =========================================================

So there is always one current version of an identity, every reader keeps
``superseded_by IS NULL``, and the history before the rebuild stays as it
was recorded. What the new rule would have made history (a correction
within the replay, a late row) is not written: the rebuild guarantees the
current state, and the batch that did it is on every row it touched.

A column type change (2026-11-04: the three Curve money columns, text to
currency) is applied here too, since the migration never alters a live
column. Before the first file is typed, every business column whose live
SQL type is not the one the spec now gives it is nulled in ``typed`` and
``curated`` and altered to the spec's type; the typed rows are then
replaced as usual, so they carry the parsed values; and before the
reconciliation every curated row of the source, current or history, takes
those columns' values from the rebuilt typed row of its own line. The
values come from the replay, never from a conversion written in SQL,
because the parsing rule has one implementation. A history row keeps the
row hash it was recorded with, which named its values under the rule of
its day; its business columns are what the current rule makes of the same
landed line, as in every layer above ``landed``.

What stops a rebuild, each of which rolls back the whole source: a file
the current spec no longer recognises; a line that would newly quarantine
or newly type (the quarantine on record would be wrong afterwards, and that
is a separate decision); control totals that disagree with the batch that
loaded the file; a conflict inside the replay; a curated row with no typed
line to refill it from; a spec column the live table lacks (the migration
runs first). ``apply=False`` runs everything but the writes and returns
the same counts, which is what a person reads before saying yes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from sbh_data_bank.commit.engine import findings_for, spec_sha256, utc_now
from sbh_data_bank.commit.records import (
    BatchRecord,
    FindingWrite,
    LayerReader,
    LoadedFile,
    TypedWrite,
    WriteContext,
)
from sbh_data_bank.commit.retype import FileReplay, replay_file_with_load
from sbh_data_bank.core.identity import new_batch_id
from sbh_data_bank.curate.delivery import Coverage
from sbh_data_bank.curate.rule import DeliveryFacts, Ledger, facts_for
from sbh_data_bank.registry.spec import CoverageSource, SourceSpec
from sbh_data_bank.store.schema import DEDUP_RULE

__all__ = [
    "REBUILT",
    "RETYPED",
    "ColumnRetype",
    "RebuildCounts",
    "RebuildRefusedError",
    "RebuildResult",
    "RebuildStore",
    "TargetRow",
    "rebuild_source",
]

#: The supersede reason a rebuild writes on a retired row.
RETYPED = "retyped"
#: The batch outcome of a rebuild. Not LOADED: it registered no file.
REBUILT = "REBUILT"


class RebuildRefusedError(Exception):
    """The replay showed something a rebuild may not write over."""


@dataclass(frozen=True, slots=True)
class TargetRow:
    """One row that is current under the new rule: where it is in
    ``typed`` and what the ledger says about it."""

    source_file_id: int
    line_number: int
    identity_hash: str
    occurrence: int
    row_hash: str
    as_of: date
    outcome: str


@dataclass(frozen=True, slots=True)
class ColumnRetype:
    """A business column whose live SQL type is not the one the spec gives
    it: ``live_type`` and ``spec_type`` are DDL tokens as ``store.schema``
    spells them (``NVARCHAR(1000)``, ``DECIMAL(38,4)``)."""

    target: str
    live_type: str
    spec_type: str

    def describe(self) -> str:
        return f"{self.target} {self.live_type}->{self.spec_type}"


@dataclass(frozen=True, slots=True)
class RebuildCounts:
    kept: int
    retired: int
    inserted: int


class RebuildStore(LayerReader, Protocol):
    """What a rebuild needs from a store beyond reading the layers back.
    Both stores implement it beside ``Store``."""

    def transaction(self): ...

    def insert_batch(self, batch: BatchRecord) -> None: ...

    def finish_batch(self, batch: BatchRecord) -> None: ...

    def replace_typed(
        self, spec: SourceSpec, ctx: WriteContext, source_file_id: int, rows: list[TypedWrite]
    ) -> None:
        """Delete the file's ``typed.*`` rows and write these in their place."""
        ...

    def replace_findings(
        self, spec: SourceSpec, ctx: WriteContext, source_file_id: int, findings: list[FindingWrite]
    ) -> None:
        """Delete the file's ``stg.duplicate_finding`` rows and write these."""
        ...

    def reconcile_curated(
        self, spec: SourceSpec, ctx: WriteContext, target: list[TargetRow], *, apply: bool
    ) -> RebuildCounts:
        """Hold the live curated rows against ``target`` (module docstring).
        With ``apply`` retire and insert; without, only count."""
        ...

    def column_retypes(self, spec: SourceSpec) -> list[ColumnRetype]:
        """The business columns whose live type in ``typed`` or ``curated``
        differs from the spec's, in ``landed_targets`` order; empty on a
        table the migration made from this spec. May raise
        ``RebuildRefusedError`` for a column the live table lacks."""
        ...

    def retype_columns(
        self, spec: SourceSpec, ctx: WriteContext, retypes: list[ColumnRetype]
    ) -> None:
        """Null these columns on every row of the tenant in ``typed`` and
        ``curated`` and alter them to the spec's type. The values come
        back from the replay (``replace_typed``, then ``refill_curated``)."""
        ...

    def refill_curated(
        self, spec: SourceSpec, ctx: WriteContext, retypes: list[ColumnRetype], *, apply: bool
    ) -> tuple[int, int]:
        """Copy these columns into every curated row of the tenant, current
        or history, from the typed row of the same line. Returns (rows with
        a typed line, rows without one); writes only with ``apply`` and
        only when every row has its line."""
        ...

    def refresh_hashes(self, spec: SourceSpec, ctx: WriteContext, *, apply: bool) -> int:
        """Bring the tenant's curated rows whose row hash differs from their
        typed line's back in step, the rows retired as ``retyped`` excepted
        (they keep the fingerprint of the spec that wrote them). Returns
        how many; writes only with ``apply``."""
        ...


@dataclass(slots=True)
class RebuildResult:
    source_key: str
    spec_sha256: str
    applied: bool
    batch_id: str | None = None
    files: int = 0
    lines: int = 0
    rehashed: int = 0
    """Lines whose row hash the new typing changed."""
    retypes: list[ColumnRetype] = field(default_factory=list)
    """Live columns altered to the spec's type (module docstring)."""
    refilled: int = 0
    """Curated rows, current and history, whose retyped columns were refilled."""
    rehashed_history: int = 0
    """Curated rows the reconcile left in place whose row hash no longer
    matched their typed line's, brought back in step (history rows, when
    a column joined the hash or changed type)."""
    counts: RebuildCounts | None = None
    refusal: str | None = None
    replays: list[FileReplay] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return self.refusal is not None

    def summary(self) -> str:
        head = (
            f"{self.source_key}: {self.files} files, {self.lines} lines, {self.rehashed} rehashed"
        )
        if self.retypes:
            head += "; retyping " + ", ".join(r.describe() for r in self.retypes)
        if self.refused:
            return f"{head}; REFUSED - {self.refusal}"
        assert self.counts is not None
        c = self.counts
        verb = "applied" if self.applied else "would apply"
        tail = f"curated kept {c.kept}, retired {c.retired}, inserted {c.inserted}"
        if self.retypes:
            tail += f", history refilled {self.refilled}"
        if self.rehashed_history:
            tail += f", history rehashed {self.rehashed_history}"
        if self.batch_id:
            tail += f" (batch {self.batch_id})"
        return f"{head}; {verb}: {tail}"


def _in_as_of_order(files: list[LoadedFile]) -> list[LoadedFile]:
    """As-of first, registration order among equals: the order the loads
    were committed in, so the replay sees what the engine saw."""
    return sorted(files, key=lambda f: (f.as_of or date.min, f.source_file_id))


def _coverage(spec: SourceSpec, file: LoadedFile) -> Coverage | None:
    if not spec.vanishes or file.coverage_to is None:
        return None
    return Coverage(file.coverage_from, file.coverage_to, spec.coverage or CoverageSource.FILENAME)


def _check(replay: FileReplay) -> None:
    """The replay must be a retyping, not a change in what is on record."""
    name = replay.file.file_name
    if replay.outcome != "LOADED":
        raise RebuildRefusedError(f"{name}: the current spec does not load it ({replay.outcome})")
    if replay.now_quarantined:
        raise RebuildRefusedError(
            f"{name}: {replay.now_quarantined} line(s) would newly quarantine; a rebuild may not "
            "lose a typed row, fix the spec or quarantine them in a load"
        )
    if replay.now_typed:
        raise RebuildRefusedError(
            f"{name}: {replay.now_typed} quarantined line(s) would now type; resolving "
            "quarantine is a separate step"
        )
    if not replay.totals_agree:
        raise RebuildRefusedError(
            f"{name}: control totals no longer agree with the batch that loaded it"
        )


def rebuild_source(
    store: RebuildStore,
    spec: SourceSpec,
    *,
    tenant_id: str,
    actor: str,
    run_id: str,
    apply: bool,
    clock: Callable[[], datetime] = utc_now,
    on_file: Callable[[FileReplay], None] | None = None,
) -> RebuildResult:
    """Rebuild one source (module docstring). One transaction: everything
    or nothing. A refusal is returned, not raised, with nothing written."""
    digest = spec_sha256(spec)
    result = RebuildResult(spec.key, digest, apply)
    files = _in_as_of_order(store.loaded_files(tenant_id, spec.key))
    now = clock()
    batch = BatchRecord(
        batch_id=new_batch_id(),
        tenant_id=tenant_id,
        run_id=run_id,
        source_key=spec.key,
        started_at=now,
        dedup_mode=DEDUP_RULE,
        detail=f"rebuild under spec {digest}",
    )
    ctx = WriteContext(
        tenant_id=tenant_id,
        batch_id=batch.batch_id,
        source_file_id=0,
        created_by=actor,
        created_at=now,
    )
    try:
        with store.transaction():
            if apply:
                store.insert_batch(batch)
            # A type change first: the typed rows written below must land in
            # columns of the type the spec declares, or SQL would convert the
            # parsed values back to text on the way in.
            retypes = store.column_retypes(spec)
            result.retypes = list(retypes)
            if retypes and apply:
                store.retype_columns(spec, ctx, retypes)
            ledger = Ledger.for_spec(spec)
            outcomes: dict[tuple[int, int], str] = {}
            for file in files:
                replay, typed, findings, facts = _retype(store, spec, file)
                _check(replay)
                result.replays.append(replay)
                result.files += 1
                result.lines += len(typed)
                result.rehashed += replay.rehashed
                if on_file is not None:
                    on_file(replay)
                if apply:
                    store.replace_typed(spec, ctx, file.source_file_id, typed)
                    store.replace_findings(spec, ctx, file.source_file_id, findings)
                assert file.as_of is not None
                delivery = DeliveryFacts(
                    str(file.source_file_id), file.as_of, _coverage(spec, file), facts
                )
                for fact, outcome in ledger.decide(delivery):
                    outcomes[(file.source_file_id, fact.line_number)] = outcome
                if ledger.apply(delivery).held:
                    raise RebuildRefusedError(
                        f"{file.file_name}: the replay meets a conflict (same as-of, different "
                        "content for one identity); a person decides"
                    )
            if retypes:
                # Count first, write after the reconcile: the refill carries
                # the row hash with the values (every line rehashes under a
                # new type), and a live row refilled before the reconcile
                # would match its replacement and be kept, which the plan,
                # which writes nothing, could not predict. After it, the
                # rule has retired the old generation and inserted the new,
                # and the refill brings the retired rows and the older
                # history up to the new types with the fingerprint to match.
                refilled, orphans = store.refill_curated(spec, ctx, retypes, apply=False)
                if orphans:
                    raise RebuildRefusedError(
                        f"{orphans} curated rows have no typed line to take their "
                        "retyped values from"
                    )
                result.refilled = refilled
            target = list(iter_target(ledger, outcomes))
            counts = store.reconcile_curated(spec, ctx, target, apply=apply)
            result.counts = counts
            if retypes and apply:
                store.refill_curated(spec, ctx, retypes, apply=True)
            # The reconcile rewrote the live rows; the history rows it left
            # in place rehash too when a column joined the hash or changed
            # type, and their fingerprint must stay true to their values.
            result.rehashed_history = store.refresh_hashes(spec, ctx, apply=apply)
            if apply:
                batch.rows_read = result.lines
                batch.rows_inserted = counts.inserted
                batch.rows_superseded = counts.retired
                batch.rows_unchanged = counts.kept
                batch.outcome = REBUILT
                batch.detail = (
                    f"rebuild under spec {digest}: {result.files} files, {result.lines} typed "
                    f"rows replaced ({result.rehashed} rehashed); curated kept {counts.kept}, "
                    f"retired {counts.retired} as {RETYPED}, inserted {counts.inserted}"
                )
                batch.finished_at = clock()
                store.finish_batch(batch)
                result.batch_id = batch.batch_id
    except RebuildRefusedError as exc:
        result.refusal = str(exc)
    return result


def _retype(store: LayerReader, spec: SourceSpec, file: LoadedFile):
    """Type one file again and reduce it to what the rebuild writes and
    what the ledger needs. The replay's rows are dropped on return, so a
    source is never held whole."""
    replay, load = replay_file_with_load(store, spec, file)
    if load is None or replay.outcome != "LOADED":
        return replay, [], [], []
    digest = spec_sha256(spec)
    typed = []
    for row in load.rows:
        values = {k: v for k, v in row.values.items() if k != "_row_hash"}
        typed.append(TypedWrite(row.line_number, digest, row.values["_row_hash"], values))
    assert file.as_of is not None
    controls = sorted(load.counters.control_totals)
    facts = facts_for(spec, load.rows, as_of=file.as_of, controls=controls)
    decided = [(fact, "inserted") for fact in facts]
    findings = findings_for(spec, decided, load)
    return replay, typed, findings, facts


def iter_target(ledger: Ledger, outcomes: dict[tuple[int, int], str]) -> Iterator[TargetRow]:
    """Exposed for tests: the target rows a ledger's current state gives."""
    for (identity_hash, occurrence), row in ledger.current.items():
        key = (int(row.delivery), row.line_number)
        yield TargetRow(
            key[0], key[1], identity_hash, occurrence, row.row_hash, row.as_of, outcomes[key]
        )
