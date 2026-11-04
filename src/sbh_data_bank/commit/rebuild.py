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

What stops a rebuild, each of which rolls back the whole source: a file
the current spec no longer recognises; a line that would newly quarantine
or newly type (the quarantine on record would be wrong afterwards, and that
is a separate decision); control totals that disagree with the batch that
loaded the file; a conflict inside the replay. ``apply=False`` runs
everything but the writes and returns the same counts, which is what a
person reads before saying yes.
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
        if self.refused:
            return f"{head}; REFUSED - {self.refusal}"
        assert self.counts is not None
        c = self.counts
        verb = "applied" if self.applied else "would apply"
        tail = f"curated kept {c.kept}, retired {c.retired}, inserted {c.inserted}"
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
            target = list(iter_target(ledger, outcomes))
            counts = store.reconcile_curated(spec, ctx, target, apply=apply)
            result.counts = counts
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
