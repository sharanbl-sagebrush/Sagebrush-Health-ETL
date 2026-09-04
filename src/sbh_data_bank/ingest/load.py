"""Loading one file: bind, read, type, account for every row.

This is where the definition-of-done invariants are enforced rather than
described. A :class:`FileLoad` is either ``ok`` - in which case every row it
read is accounted for and the rows are safe to commit - or it carries the
reason it is not, and nothing commits.

Nothing here touches a database. A load produces typed rows, quarantined rows
and counters; persisting them is the next layer's job. That separation is what
makes the whole thing testable against real files without a server.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO

from sbh_data_bank.core.identity import file_sha256, stable_row_hash
from sbh_data_bank.ingest.reader import (
    QuarantinedRow,
    ReadCounters,
    TypedRow,
    open_source,
    resolve_sheet,
)
from sbh_data_bank.ingest.reader import type_row as _type_row
from sbh_data_bank.registry.binding import Binding, NoVariantMatchedError, bind
from sbh_data_bank.registry.headers import HeaderIndex
from sbh_data_bank.registry.spec import FooterPolicy, SourceSpec
from sbh_data_bank.transforms.ndc import NdcDirectory
from sbh_data_bank.transforms.values import parse_currency

__all__ = ["FileLoad", "FooterCheck", "LoadOutcome", "load_file", "load_workbook"]


class LoadOutcome:
    """Why a load ended as it did. Strings so they persist legibly."""

    LOADED = "LOADED"
    RECOGNISED_NOT_LOADED = "RECOGNISED_NOT_LOADED"
    """The variant is deliberately not ingested - the superseded Cardinal
    invoice summary, for instance."""
    UNRECOGNISED = "UNRECOGNISED"
    BLOCKED_BY_DRIFT = "BLOCKED_BY_DRIFT"
    CONTROL_TOTAL_MISMATCH = "CONTROL_TOTAL_MISMATCH"
    """The file states its own totals and ours disagree. Either rows were lost
    or values were misparsed; either way nothing commits."""

    UNBALANCED = "UNBALANCED"
    """Rows read do not equal rows accounted for. Always a bug in this code,
    never a data problem, and never allowed to commit."""


@dataclass(slots=True)
class FileLoad:
    """Everything one file produced."""

    source_key: str
    filename: str
    outcome: str
    counters: ReadCounters = field(default_factory=ReadCounters)
    rows: list[TypedRow] = field(default_factory=list)
    quarantined: list[QuarantinedRow] = field(default_factory=list)
    binding: Binding | None = None
    sheet: str | None = None
    file_hash: str | None = None
    footer: FooterCheck | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Whether these rows may be committed."""
        return self.outcome == LoadOutcome.LOADED and self.counters.balances

    @property
    def variant_name(self) -> str | None:
        return self.binding.variant.name if self.binding else None

    def quarantine_rate(self) -> float:
        if not self.counters.rows_read:
            return 0.0
        return self.counters.rows_quarantined / self.counters.rows_read

    def summary(self) -> str:
        parts = [f"{self.filename}", f"[{self.outcome}]"]
        if self.variant_name:
            parts.append(f"variant={self.variant_name}")
        counters = self.counters
        parts.append(
            f"read={counters.rows_read} ok={counters.rows_ok} "
            f"quarantined={counters.rows_quarantined}"
        )
        for name, total in sorted(counters.control_totals.items()):
            parts.append(f"{name}={total:,}")
        if self.footer is not None:
            parts.append("| " + self.footer.describe())
        if self.detail:
            parts.append(f"- {self.detail}")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class FooterCheck:
    """The totals the file states for itself, against the ones we computed.

    Worth far more than it costs. The vendor performed this arithmetic
    independently, so agreement is real evidence that every row was read and
    every value parsed - not merely that the code did not crash.
    """

    line_number: int
    declared: dict[str, Decimal]
    computed: dict[str, Decimal]

    @property
    def mismatches(self) -> dict[str, tuple[Decimal, Decimal]]:
        return {
            name: (declared, self.computed.get(name, Decimal(0)))
            for name, declared in self.declared.items()
            if declared != self.computed.get(name, Decimal(0))
        }

    @property
    def agrees(self) -> bool:
        return not self.mismatches

    def describe(self) -> str:
        if self.agrees:
            joined = ", ".join(f"{k}={v:,}" for k, v in sorted(self.declared.items()))
            return "file totals agree: " + joined
        joined = "; ".join(
            f"{k} declared {d:,} computed {c:,}" for k, (d, c) in sorted(self.mismatches.items())
        )
        return "file totals disagree: " + joined


def _control_columns(binding: Binding) -> list[str]:
    """Columns the spec marks as additive control totals.

    A control total turns "the load succeeded" into "the load moved the amount
    the file contained", which is the difference that matters - a silent parse
    failure in money is the defect with the largest consequence, and it is how
    227,284 credit rows became null and inflated a payments total by $39.97M.

    Only columns explicitly flagged, never every currency column. Summing a
    unit price produces a number with no meaning, and a meaningless control
    total is worse than none because somebody will try to reconcile against it.
    """
    return [b.column.target for b in binding.landed if b.column.control_total]


def _cell_text(row: Any, position: int) -> str:
    value = row.values[position] if position < len(row.values) else None
    return "" if value is None else str(value).strip()


def _positions_for(binding: Binding, targets) -> list[int]:
    return [
        b.match.position
        for b in binding.columns
        if b.match is not None and b.column.target in targets
    ]


def _looks_like_footer(row: Any, binding: Binding, controls: list[str]) -> bool:
    """A totals line: every natural-key column blank, some control column not.

    Deliberately strict on both halves. A data row that happens to be missing
    its key is a real problem and must quarantine, so a blank key alone is not
    enough - a genuine footer also carries the totals that make it a footer.
    """
    key_positions = _positions_for(binding, set(binding.spec.natural_key))
    if not key_positions:
        return False
    if any(_cell_text(row, p) for p in key_positions):
        return False
    return any(_cell_text(row, p) for p in _positions_for(binding, set(controls)))


def _read_footer_totals(row: Any, binding: Binding, controls: list[str]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for column_binding in binding.columns:
        if column_binding.column.target not in controls or column_binding.match is None:
            continue
        parsed = parse_currency(_cell_text(row, column_binding.match.position))
        if parsed.ok and parsed.value is not None:
            totals[column_binding.column.target] = parsed.value
    return totals


def _load_stream(
    spec: SourceSpec,
    binding: Binding,
    rows: Iterator[Any],
    *,
    filename: str,
    sheet: str | None,
    ndc_directory: NdcDirectory | None,
    row_hash_fields: list[str] | None,
) -> FileLoad:
    load = FileLoad(spec.key, filename, LoadOutcome.LOADED, binding=binding, sheet=sheet)

    if not binding.variant.loads:
        load.outcome = LoadOutcome.RECOGNISED_NOT_LOADED
        load.detail = (
            f"variant {binding.variant.name!r} is superseded by "
            f"{binding.variant.superseded_by!r}; nothing loaded"
        )
        return load

    blocking = binding.blocking_problem()
    if blocking is not None:
        load.outcome = LoadOutcome.BLOCKED_BY_DRIFT
        load.detail = blocking
        return load

    controls = _control_columns(binding)
    totals: dict[str, Decimal] = {c: Decimal(0) for c in controls}
    injected = {binding.variant.read.sheet_name_column: sheet} if sheet else {}
    hash_fields = row_hash_fields or [b.column.target for b in binding.landed]
    expects_footer = binding.variant.read.footer is FooterPolicy.TOTALS_ROW

    def ingest(raw: Any) -> None:
        load.counters.rows_read += 1
        typed = _type_row(raw, binding, ndc_directory=ndc_directory, injected=injected)
        if isinstance(typed, QuarantinedRow):
            load.counters.rows_quarantined += 1
            load.quarantined.append(typed)
            return
        load.counters.rows_ok += 1
        typed.values["_row_hash"] = stable_row_hash(typed.values, hash_fields)
        load.rows.append(typed)
        for column in controls:
            value = typed.values.get(column)
            if isinstance(value, Decimal):
                totals[column] += value

    # One row of lookahead, so the last row can be inspected before it is
    # committed to. Only the final row can be a footer, and the file is
    # streamed, so a single held row is all the buffering needed.
    held: Any = None
    for raw in rows:
        if held is not None:
            ingest(held)
        held = raw
    if held is not None:
        if expects_footer and _looks_like_footer(held, binding, controls):
            load.footer = FooterCheck(
                line_number=held.line_number,
                declared=_read_footer_totals(held, binding, controls),
                computed=dict(totals),
            )
        else:
            ingest(held)

    load.counters.control_totals = totals

    if not load.counters.balances:
        load.outcome = LoadOutcome.UNBALANCED
        load.detail = load.counters.imbalance() or ""
    elif load.footer is not None and not load.footer.agrees:
        load.outcome = LoadOutcome.CONTROL_TOTAL_MISMATCH
        load.detail = load.footer.describe()
    return load


def _rewind(source: Path | BinaryIO) -> bool:
    """Return the source to the start, or say it cannot be."""
    if isinstance(source, Path):
        return True
    if source.seekable():
        source.seek(0)
        return True
    return False


def load_file(
    source: Path | BinaryIO,
    spec: SourceSpec,
    *,
    filename: str | None = None,
    sheet: str | None = None,
    ndc_directory: NdcDirectory | None = None,
    compute_file_hash: bool = True,
) -> FileLoad:
    """Load one CSV, or one worksheet of one workbook.

    ``filename`` is required when ``source`` is a stream, since a zip member or
    a blob has no path of its own.

    Variants are probed in order, each with its own read settings, because
    finding the header can itself depend on the variant: the ABC extract has a
    ``sep=,`` line and a report title above its header as a CSV, and one title
    row as a workbook. Probing stops at the first variant that both reads and
    binds, so the common case costs one open.

    A non-seekable stream can only be probed once, so it is read with the first
    variant's settings. Zip members and local files are seekable; this only
    constrains a future network source, and it fails loudly rather than
    silently mis-reading.
    """
    if filename is None:
        if not isinstance(source, Path):
            raise ValueError("filename is required when loading from a stream")
        filename = source.name

    attempts: list[tuple] = []
    last_error: str = ""

    for variant in spec.variants:
        if attempts and not _rewind(source):
            break  # unseekable stream: one attempt only
        try:
            header, rows = open_source(source, variant.read, sheet=sheet)
        except (StopIteration, UnicodeDecodeError, KeyError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        try:
            binding = bind(spec, HeaderIndex(header))
        except NoVariantMatchedError as exc:
            last_error = str(exc)
            continue
        attempts.append((binding, rows))
        if binding.variant.name == variant.name:
            break  # read settings and bound variant agree

    if not attempts:
        return FileLoad(
            spec.key, filename, LoadOutcome.UNRECOGNISED, detail=last_error, sheet=sheet
        )

    binding, rows = attempts[-1]
    load = _load_stream(
        spec,
        binding,
        rows,
        filename=filename,
        sheet=sheet,
        ndc_directory=ndc_directory,
        row_hash_fields=None,
    )
    if compute_file_hash and isinstance(source, Path):
        load.file_hash = file_sha256(source)
    return load


def load_workbook(
    path: Path,
    spec: SourceSpec,
    *,
    ndc_directory: NdcDirectory | None = None,
) -> list[FileLoad]:
    """Load every worksheet of a workbook that any variant selects.

    One workbook can hold several logical datasets - the Master COGS List has a
    consolidated sheet and five per-wholesaler tabs, which bind to different
    variants of the same spec. Each sheet is its own load with its own row
    accounting, so one bad tab does not obscure the rest.
    """
    wanted: list[str] = []
    for variant in spec.variants:
        for name in resolve_sheet(path, variant.read):
            if name not in wanted:
                wanted.append(name)

    loads = []
    for name in wanted:
        load = load_file(
            path,
            spec,
            filename=path.name,
            sheet=name,
            ndc_directory=ndc_directory,
            compute_file_hash=False,
        )
        loads.append(load)
    if loads:
        digest = file_sha256(path)
        for load in loads:
            load.file_hash = digest
    return loads
