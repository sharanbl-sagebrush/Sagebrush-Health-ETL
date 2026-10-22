"""What the commit engine writes, and the store interface it writes through.

The engine (``commit.engine``) decides; a :class:`Store` persists. Keeping
the two apart is what lets the engine be tested end to end without Azure:
``commit.memory.MemoryStore`` holds the same tables in dictionaries, and
``commit.sql.SqlStore`` writes the T-SQL for the schema ``store.schema``
generates. Both implement this protocol and nothing else.

Every record here is the row shape of one table in ``store.schema``, by
name: :class:`SourceFileRecord` is ``meta.source_file``, :class:`BatchRecord`
is ``meta.batch``, and the ``*Write`` records are one row each of the
``landed``, ``typed``, ``curated``, ``stg.quarantine`` and
``stg.duplicate_finding`` tables. The audit columns (decision D11) come
from the :class:`WriteContext` the engine passes with every write, so a row
can never be written under the wrong batch or actor.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from sbh_data_bank.curate.delivery import Coverage
from sbh_data_bank.curate.rule import CurrentRow
from sbh_data_bank.ingest.replay import LandedLine
from sbh_data_bank.registry.spec import SourceSpec

__all__ = [
    "BatchRecord",
    "CuratedWrite",
    "CurrentQuery",
    "FindingWrite",
    "FoundFile",
    "LandedWrite",
    "LayerReader",
    "LoadedFile",
    "QuarantineWrite",
    "RunRecord",
    "SourceFileRecord",
    "Store",
    "StoredCurrent",
    "Supersession",
    "TypedWrite",
    "WriteBatch",
    "WriteContext",
    "iter_values",
]


@dataclass(slots=True)
class RunRecord:
    """One row of ``meta.run``: a pipeline execution grouping its batches."""

    run_id: str
    tenant_id: str
    trigger_kind: str
    started_at: datetime
    finished_at: datetime | None = None
    outcome: str | None = None
    detail: str | None = None


@dataclass(slots=True)
class SourceFileRecord:
    """One row of ``meta.source_file``: a physical file (or one worksheet of
    one), identified by content hash. ``source_file_id`` is assigned by the
    store on insert."""

    tenant_id: str
    source_key: str
    file_name: str
    file_sha256: str
    first_seen_at: datetime
    file_bytes: int | None = None
    sheet_name: str | None = None
    delivery: str | None = None
    delivery_rank: int = 0
    as_of: date | None = None
    as_of_source: str | None = None
    coverage_from: date | None = None
    coverage_to: date | None = None
    header_json: str | None = None
    source_file_id: int | None = None


@dataclass(slots=True)
class BatchRecord:
    """One row of ``meta.batch``: one attempt to load one file. Inserted when
    the attempt starts and finished with the counts and the outcome."""

    batch_id: str
    tenant_id: str
    run_id: str
    source_key: str
    started_at: datetime
    source_file_id: int | None = None
    variant_name: str | None = None
    dedup_mode: str | None = None
    outcome: str = "STARTED"
    detail: str | None = None
    rows_landed: int = 0
    rows_read: int = 0
    rows_inserted: int = 0
    rows_superseded: int = 0
    rows_unchanged: int = 0
    rows_late: int = 0
    rows_quarantined: int = 0
    rows_conflict: int = 0
    rows_vanished: int = 0
    control_totals: str | None = None
    finished_at: datetime | None = None

    @property
    def accounted(self) -> int:
        """inserted + superseded + unchanged + late + quarantined, which must
        equal ``rows_read`` on a committed batch (the ``meta.batch`` rule)."""
        return (
            self.rows_inserted
            + self.rows_superseded
            + self.rows_unchanged
            + self.rows_late
            + self.rows_quarantined
        )


@dataclass(frozen=True, slots=True)
class FoundFile:
    """A file already registered under a content hash: its id, whether any
    batch committed it (then it is a duplicate), and how many attempts there
    have been, so a replayed re-attempt still gets a distinct batch id."""

    source_file_id: int
    committed: bool
    attempts: int


@dataclass(frozen=True, slots=True)
class WriteContext:
    """Who is writing, under which batch and file, and when: the audit
    columns every layered row carries (decision D11)."""

    tenant_id: str
    batch_id: str
    source_file_id: int
    created_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LandedWrite:
    """One ``landed.*`` row: every cell as delivered text, by target."""

    line_number: int
    cells: dict[str, str | None]
    extra_cells: str | None
    """JSON of vendor header -> text for the columns no spec column claimed,
    or ``None`` when there were none on this line."""


@dataclass(frozen=True, slots=True)
class TypedWrite:
    """One ``typed.*`` row: the parsed values, the spec that typed them and
    the content hash."""

    line_number: int
    spec_sha256: str
    row_hash: str
    values: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CuratedWrite:
    """One ``curated.*`` row. A late row is written already superseded, with
    ``superseded_by`` naming the batch whose row beat it."""

    line_number: int
    identity_hash: str
    occurrence: int
    row_hash: str
    as_of: date
    outcome: str
    values: dict[str, Any]
    superseded_by: str | None = None
    superseded_at: datetime | None = None
    supersede_reason: str | None = None


@dataclass(frozen=True, slots=True)
class QuarantineWrite:
    """One ``stg.quarantine`` row."""

    line_number: int
    reasons: str
    errors: str
    payload: str


@dataclass(frozen=True, slots=True)
class FindingWrite:
    """One ``stg.duplicate_finding`` row: an identity that several lines of
    one file share."""

    key_values: str
    line_numbers: str
    identical: bool


@dataclass(frozen=True, slots=True)
class Supersession:
    """Retire one live curated row: which (by its stored handle), why, and
    the batch doing it is on the call."""

    ref: Any
    reason: str


@dataclass(frozen=True, slots=True)
class StoredCurrent:
    """A live curated row as the store returns it, keyed for the ledger."""

    identity_hash: str
    occurrence: int
    row: CurrentRow

    @property
    def key(self) -> tuple[str, int]:
        return (self.identity_hash, self.occurrence)


@dataclass(frozen=True, slots=True)
class CurrentQuery:
    """Which live rows the engine needs to decide one delivery: every row
    sharing an incoming identity, plus (for vanish) every row whose coverage
    column falls inside the delivery's window. The column names say how to
    build the :class:`CurrentRow` the ledger compares on."""

    keys: frozenset[tuple[str, int]]
    coverage: Coverage | None
    coverage_column: str | None
    event_column: str | None
    scope_columns: tuple[str, ...]
    control_columns: tuple[str, ...]
    snapshot_as_of_is_event_date: bool = False
    """For a snapshot source the ledger's event date is the row's as-of."""


@dataclass(slots=True)
class WriteBatch:
    """Everything one committed load writes to the layered tables, gathered
    so a store can write it in one pass."""

    landed: list[LandedWrite] = field(default_factory=list)
    typed: list[TypedWrite] = field(default_factory=list)
    curated: list[CuratedWrite] = field(default_factory=list)
    quarantine: list[QuarantineWrite] = field(default_factory=list)
    findings: list[FindingWrite] = field(default_factory=list)
    supersessions: list[Supersession] = field(default_factory=list)


class Store(Protocol):
    """What the engine needs from a database. One implementation writes
    T-SQL, one writes dictionaries; the engine cannot tell them apart."""

    def transaction(self) -> AbstractContextManager[None]:
        """One unit of work. Everything inside commits together or not at
        all; an exception rolls the whole thing back."""
        ...

    def insert_run(self, run: RunRecord) -> None: ...

    def finish_run(self, run: RunRecord) -> None: ...

    def find_source_file(
        self, tenant_id: str, source_key: str, file_sha256: str, sheet_name: str | None
    ) -> FoundFile | None:
        """The file this source already registered under this content hash,
        or ``None``. A committed batch is one with outcome LOADED; a file
        refused or held before is not a duplicate. The same bytes seen by
        another source are that source's own file: two specs can share one
        workbook (Cardinal's summary and detail) and each decides for itself."""
        ...

    def insert_source_file(self, record: SourceFileRecord) -> int:
        """Register a file and return its id."""
        ...

    def delivery_rank(self, tenant_id: str, source_key: str, as_of: date) -> int:
        """1 + the number of this source's registered files with an earlier
        as-of: the file's position in as-of order when it was registered."""
        ...

    def insert_batch(self, batch: BatchRecord) -> None: ...

    def finish_batch(self, batch: BatchRecord) -> None: ...

    def current_rows(
        self, spec: SourceSpec, tenant_id: str, query: CurrentQuery
    ) -> list[StoredCurrent]:
        """The live curated rows a delivery has to be decided against."""
        ...

    def write(self, spec: SourceSpec, ctx: WriteContext, batch: WriteBatch) -> None:
        """Write one committed load's rows: landed, typed, curated inserts,
        quarantine, findings, then the supersessions, under ``ctx``."""
        ...


@dataclass(frozen=True, slots=True)
class LoadedFile:
    """A file with a LOADED batch, as a :class:`LayerReader` lists it: what
    the replay needs to type it again and to compare the result."""

    source_file_id: int
    file_name: str
    sheet_name: str | None
    header_json: str
    batch_id: str
    control_totals: str | None
    as_of: date | None = None


class LayerReader(Protocol):
    """Reading the layers back, for ``commit.retype``: the loaded files of
    a source, each file's landed rows, and what its load typed and
    quarantined. Both stores implement it beside :class:`Store`."""

    def loaded_files(self, tenant_id: str, source_key: str) -> list[LoadedFile]:
        """Files with a LOADED batch, in registration order, one entry per
        worksheet of a workbook."""
        ...

    def landed_lines(self, spec: SourceSpec, source_file_id: int) -> Iterator[LandedLine]:
        """The file's ``landed.*`` rows in line order."""
        ...

    def typed_hashes(self, spec: SourceSpec, source_file_id: int) -> dict[int, str]:
        """``line_number -> row_hash`` for the file's ``typed.*`` rows."""
        ...

    def quarantined_lines(self, batch_id: str) -> dict[int, str]:
        """``line_number -> reasons`` for the batch's ``stg.quarantine`` rows."""
        ...


def iter_values(values: dict[str, Any], columns: Sequence[str]) -> Iterator[Any]:
    """The values of ``columns`` in order, ``None`` for any the row lacks.
    How every store turns a by-target dict into a positional row."""
    for column in columns:
        yield values.get(column)
