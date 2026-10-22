"""Loading one file: bind, read, type, account for every row.

This is where the definition-of-done invariants are enforced rather than
described. A :class:`FileLoad` is either ``ok`` - in which case every row it
read is accounted for and the rows are safe to commit - or it carries the
reason it is not, and nothing commits.

Nothing here touches a database. A load produces typed rows, quarantined rows
and counters; persisting them is the next layer's job. That separation is what
makes the whole thing testable against real files without a server.

How this fits
-------------
``load_file`` is the entry point for one CSV or one worksheet; ``load_workbook``
fans out over the worksheets a spec selects. Both are called today only by
``tests/unit/test_load.py`` and, via the reader, by
``scripts/bind_real_files.py``. The sequence for one file is:

1. ``ingest.reader.open_source`` finds the header and streams raw rows.
2. ``registry.binding.bind`` picks the variant and maps spec columns to file
   positions (``Binding``).
3. ``ingest.reader.type_row`` turns each raw row into a ``TypedRow`` or a
   ``QuarantinedRow``.
4. This module keeps the counters, computes row hashes and control totals,
   recognises a totals footer, and decides the ``LoadOutcome``.

The commit engine (``sbh_data_bank.commit``) takes an ``ok`` ``FileLoad``
and writes ``meta.source_file``, ``meta.batch``, the layered ``landed.*``,
``typed.*`` and ``curated.*`` rows and ``stg.quarantine`` inside one
transaction, applying the dedup rule (``store.schema.DEDUP_RULE``). For that
it needs every file line as delivered, so a load also keeps
:class:`LandedRow`s (``keep_landed``), footer included, and the engine
holds a file whose ``FileLoad.quarantine_rate`` exceeds the spec's
``max_quarantine_rate`` (D17). ``ingest.replay`` builds the same
``FileLoad`` from the landed layer instead of a file.
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
from sbh_data_bank.registry.spec import FooterPolicy, PiiClass, SourceSpec
from sbh_data_bank.transforms.ndc import NdcDirectory
from sbh_data_bank.transforms.values import parse_currency

__all__ = ["FileLoad", "FooterCheck", "LandedRow", "LoadOutcome", "load_file", "load_workbook"]


class LoadOutcome:
    """Why a load ended as it did. Strings so they persist legibly.

    These become ``meta.batch.outcome``. Only ``LOADED`` (together with
    balanced counters) permits a commit; every other value means the file's
    rows stay out of the warehouse and ``FileLoad.detail`` says why.
    """

    LOADED = "LOADED"
    """Every row typed or quarantined, counters balance, footer (if any)
    agrees. The rows may commit."""
    RECOGNISED_NOT_LOADED = "RECOGNISED_NOT_LOADED"
    """The variant is deliberately not ingested - the superseded Cardinal
    invoice summary, for instance."""
    UNRECOGNISED = "UNRECOGNISED"
    """No variant's read settings and detector both fit the file. Either a
    new layout from the vendor or the wrong spec; an operator has to look."""
    BLOCKED_BY_DRIFT = "BLOCKED_BY_DRIFT"
    """The variant matched but a required column is missing or an unknown
    column is present, and the spec's drift policy says fail rather than
    warn. Nothing is silently dropped."""
    CONTROL_TOTAL_MISMATCH = "CONTROL_TOTAL_MISMATCH"
    """The file states its own totals and ours disagree. Either rows were lost
    or values were misparsed; either way nothing commits."""

    UNBALANCED = "UNBALANCED"
    """Rows read do not equal rows accounted for. Always a bug in this code,
    never a data problem, and never allowed to commit."""


@dataclass(frozen=True, slots=True)
class LandedRow:
    """One file line exactly as delivered, keyed by spec target.

    What ``landed.*`` stores: the text of every cell a landed column bound
    to, ``None`` where the column is absent from this file, and the cells no
    spec column claimed under their vendor header in ``extra_cells``. Built
    for every line the reader yields, including a totals footer and an
    interior blank line, because the landed layer proves what the vendor
    sent. A prohibited column's cell is never read into it.
    """

    line_number: int
    cells: dict[str, str | None]
    extra_cells: dict[str, str]
    sheet: str | None = None


@dataclass(slots=True)
class FileLoad:
    """Everything one file produced.

    The unit of work handed to the (future) commit engine. One ``FileLoad``
    per CSV, or per worksheet of a workbook, so that one bad tab in the
    Master COGS List does not hide the state of the other five.
    """

    source_key: str
    """The spec key, e.g. ``wholesaler.cardinal.invoice``."""
    filename: str
    """Name only - a zip member or blob has no path."""
    outcome: str
    """One of the :class:`LoadOutcome` strings."""
    counters: ReadCounters = field(default_factory=ReadCounters)
    """read / ok / quarantined counts and the control-total sums."""
    rows: list[TypedRow] = field(default_factory=list)
    """Rows eligible to commit. Each carries ``_row_hash`` in its values."""
    quarantined: list[QuarantinedRow] = field(default_factory=list)
    """Rows that failed typing, with reasons and raw payload."""
    landed: list[LandedRow] = field(default_factory=list)
    """Every line as delivered, when the load was asked to keep them
    (``keep_landed``). Typed rows + quarantined rows + the footer line."""
    header: tuple[str, ...] = ()
    """The header row as delivered, junk columns included, in file order."""
    binding: Binding | None = None
    """How spec columns mapped to file positions; ``None`` when unrecognised."""
    sheet: str | None = None
    """Worksheet name for a workbook load."""
    file_hash: str | None = None
    """SHA-256 of the file's bytes - the identity in ``meta.source_file``."""
    footer: FooterCheck | None = None
    """The vendor's totals row against ours, when the spec declares one."""
    detail: str = ""
    """Free-text reason for a non-LOADED outcome. Becomes ``meta.batch.detail``."""

    @property
    def ok(self) -> bool:
        """Whether these rows may be committed."""
        # Both halves matter: LOADED says nothing structural went wrong, and
        # balances says the counters prove every row went somewhere.
        return self.outcome == LoadOutcome.LOADED and self.counters.balances

    @property
    def variant_name(self) -> str | None:
        """Which variant matched, for the batch record. ``None`` if none did."""
        return self.binding.variant.name if self.binding else None

    def quarantine_rate(self) -> float:
        """Fraction of rows read that were quarantined, 0.0 for an empty file.

        The commit engine holds the file when this exceeds the spec's
        ``max_quarantine_rate`` (D17): a wrong date format quarantines 100%
        of a file, and that stops the load rather than filling the
        quarantine table behind a hash that would then be a duplicate.
        """
        if not self.counters.rows_read:
            return 0.0
        return self.counters.rows_quarantined / self.counters.rows_read

    def summary(self) -> str:
        """One log line describing the load: outcome, variant, counts,
        control totals, footer agreement and any detail."""
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

    Only two sources carry a footer (file-audit.md): Cardinal Invoice Review
    and McKesson Purchasing Detail. For McKesson the footer declares extended
    price $20,394,456.72, net quantity 11,378 and more; landing it as data
    would add a phantom $20.4M purchase, so it is checked instead.
    """

    line_number: int
    """Where the footer was in the file, so a mismatch can be inspected."""
    declared: dict[str, Decimal]
    """Control column -> the figure the vendor printed."""
    computed: dict[str, Decimal]
    """Control column -> the sum of that column over the rows we typed."""

    @property
    def mismatches(self) -> dict[str, tuple[Decimal, Decimal]]:
        """Columns where declared != computed, as ``{column: (declared,
        computed)}``. A declared column we did not compute counts as a
        mismatch against zero rather than being ignored."""
        return {
            name: (declared, self.computed.get(name, Decimal(0)))
            for name, declared in self.declared.items()
            if declared != self.computed.get(name, Decimal(0))
        }

    @property
    def agrees(self) -> bool:
        """True when every declared total matches ours exactly. Exactly:
        these are Decimals, so there is no tolerance and none is needed."""
        return not self.mismatches

    def describe(self) -> str:
        """A log-friendly sentence listing the totals or the disagreements."""
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
    # `landed` already excludes PROHIBITED columns, so a control total can
    # never be computed over a value that must not be read.
    return [b.column.target for b in binding.landed if b.column.control_total]


def _cell_text(row: Any, position: int) -> str:
    """The stripped text of one cell by file position, or ``""`` when the
    row is too short or the cell is ``None``. Used only for footer detection,
    where "is there anything here" is the whole question."""
    value = row.values[position] if position < len(row.values) else None
    return "" if value is None else str(value).strip()


def _positions_for(binding: Binding, targets) -> list[int]:
    """File positions of the given target columns, skipping any that did not
    bind in this file (an optional column the vendor omitted)."""
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
        # No natural key bound means there is nothing to be "blank", so a
        # footer cannot be recognised. Such a row will quarantine instead.
        return False
    if any(_cell_text(row, p) for p in key_positions):
        return False  # a key is populated: this is a data row
    return any(_cell_text(row, p) for p in _positions_for(binding, set(controls)))


def _read_footer_totals(row: Any, binding: Binding, controls: list[str]) -> dict[str, Decimal]:
    """Parse the vendor's figures out of a footer row, one per control column.

    Everything goes through ``parse_currency`` even for quantity columns,
    because it is the most permissive of the numeric parsers (accepts ``$``,
    thousands separators and parenthesised negatives) and a footer is written
    for a human, not a parser. A cell that will not parse is simply absent
    from the result rather than failing the load; the computed total for that
    column then has nothing to be compared against.
    """
    totals: dict[str, Decimal] = {}
    for column_binding in binding.columns:
        if column_binding.column.target not in controls or column_binding.match is None:
            continue
        parsed = parse_currency(_cell_text(row, column_binding.match.position))
        if parsed.ok and parsed.value is not None:
            totals[column_binding.column.target] = parsed.value
    return totals


def _landed_row(raw: Any, binding: Binding, extra_positions: list[tuple[int, str]]) -> LandedRow:
    """The as-delivered text of one line, by target, plus the unclaimed cells.

    A cell past the end of a short row is ``None``, as in ``type_row``. Only
    the positions of landed (non-prohibited) bindings are read, so an SSN
    cell is never indexed out of the raw tuple here either.
    """
    cells: dict[str, str | None] = {}
    for cb in binding.columns:
        if cb.column.pii is PiiClass.PROHIBITED:
            continue
        if cb.match is None:
            cells[cb.column.target] = None
            continue
        position = cb.match.position
        value = raw.values[position] if position < len(raw.values) else None
        cells[cb.column.target] = None if value is None else str(value)
    extra: dict[str, str] = {}
    for position, header in extra_positions:
        value = raw.values[position] if position < len(raw.values) else None
        if value is not None and str(value) != "":
            extra[header] = str(value)
    return LandedRow(raw.line_number, cells, extra, sheet=raw.sheet)


def _extra_positions(header: tuple[str, ...], binding: Binding) -> list[tuple[int, str]]:
    """File positions of the columns no spec column claimed (``new_columns``),
    with their vendor header, so their cells can land in ``extra_cells``."""
    claimed = {cb.match.position for cb in binding.columns if cb.match is not None}
    wanted = set(binding.new_columns)
    return [(i, h) for i, h in enumerate(header) if i not in claimed and h in wanted]


def _load_stream(
    spec: SourceSpec,
    binding: Binding,
    rows: Iterator[Any],
    *,
    filename: str,
    sheet: str | None,
    ndc_directory: NdcDirectory | None,
    row_hash_fields: list[str] | None,
    header: tuple[str, ...] = (),
    keep_landed: bool = True,
) -> FileLoad:
    """Consume a raw-row stream against a settled binding and build the load.

    Everything after "which variant is this" happens here: typing each row,
    the counters, row hashes, control totals, footer recognition and the
    final outcome. Returns a :class:`FileLoad`; never raises on data.
    ``row_hash_fields`` overrides which target columns feed the row hash
    (``None`` means every landed column).
    """
    load = FileLoad(
        spec.key, filename, LoadOutcome.LOADED, binding=binding, sheet=sheet, header=header
    )

    # Gate 1: a recognise_only variant (the Cardinal detail spec meeting the
    # summary workbook, and vice versa). We know what the file is, and this
    # spec deliberately does not load it: a sibling variant supersedes it,
    # or another source's spec is the one that loads it.
    if not binding.variant.loads:
        load.outcome = LoadOutcome.RECOGNISED_NOT_LOADED
        if binding.variant.loaded_by:
            reason = f"is loaded by source {binding.variant.loaded_by!r}"
        else:
            reason = f"is superseded by {binding.variant.superseded_by!r}"
        load.detail = f"variant {binding.variant.name!r} {reason}; nothing loaded"
        return load

    # Gate 2: schema drift the spec's policy says must block. The binder has
    # already worked out what is missing or unexpected; this just asks whether
    # the policy (on_missing_column / on_new_column) allows the load to go on.
    blocking = binding.blocking_problem()
    if blocking is not None:
        load.outcome = LoadOutcome.BLOCKED_BY_DRIFT
        load.detail = blocking
        return load

    controls = _control_columns(binding)
    totals: dict[str, Decimal] = {c: Decimal(0) for c in controls}
    # The worksheet name is the one reader-injected column today. It lands
    # under the target the spec names in read.sheet_name_column.
    injected = {binding.variant.read.sheet_name_column: sheet} if sheet else {}
    # The row hash runs over the union of every loading variant's landed
    # columns (decision D6), not over the columns this file happens to have.
    # A column the legacy WeInfuse export lacks encodes as null on its rows,
    # so the same patient row from the old and the new module hash
    # identically, and a column the vendor recomputes at extraction time
    # (``row_hash_exclude``) is stored without counting as a change.
    hash_fields = row_hash_fields or list(spec.row_hash_columns)
    expects_footer = binding.variant.read.footer is FooterPolicy.TOTALS_ROW
    extra_positions = _extra_positions(header, binding) if keep_landed else []

    def land(raw: Any) -> None:
        # Every line the reader yields lands, whatever typing makes of it.
        if keep_landed:
            load.landed.append(_landed_row(raw, binding, extra_positions))

    def ingest(raw: Any) -> None:
        # One raw row in; exactly one counter goes up. This is what makes
        # `balances` hold by construction.
        land(raw)
        load.counters.rows_read += 1
        typed = _type_row(raw, binding, ndc_directory=ndc_directory, injected=injected)
        if isinstance(typed, QuarantinedRow):
            load.counters.rows_quarantined += 1
            load.quarantined.append(typed)
            return
        load.counters.rows_ok += 1
        # Content identity, computed once here so the commit engine and the
        # dedup policy never have to re-derive it.
        typed.values["_row_hash"] = stable_row_hash(typed.values, hash_fields)
        load.rows.append(typed)
        # Running control totals, over ok rows only. Quarantined rows are not
        # in the warehouse, so they are not in our side of the comparison.
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
        # The last row: footer or data? Only if the spec expects a footer AND
        # the row has the footer shape. A spec that expects none still
        # quarantines a stray totals line as BLANK_ROW / missing key, which is
        # the right outcome - it is not data.
        if expects_footer and _looks_like_footer(held, binding, controls):
            land(held)  # the footer is a file line: it lands, it is not a row
            load.footer = FooterCheck(
                line_number=held.line_number,
                declared=_read_footer_totals(held, binding, controls),
                computed=dict(totals),
            )
        else:
            ingest(held)

    load.counters.control_totals = totals

    # Final outcome, checked in order of severity. An unbalanced count is a
    # loader bug and outranks everything; a footer disagreement means the data
    # we would commit is not the data the vendor sent.
    if not load.counters.balances:
        load.outcome = LoadOutcome.UNBALANCED
        load.detail = load.counters.imbalance() or ""
    elif load.footer is not None and not load.footer.agrees:
        load.outcome = LoadOutcome.CONTROL_TOTAL_MISMATCH
        load.detail = load.footer.describe()
    return load


def _rewind(source: Path | BinaryIO) -> bool:
    """Return the source to the start, or say it cannot be."""
    # A Path is re-opened fresh on every read, so there is nothing to rewind.
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
    keep_landed: bool = True,
) -> FileLoad:
    """Load one CSV, or one worksheet of one workbook.

    ``filename`` is required when ``source`` is a stream, since a zip member or
    a blob has no path of its own. ``keep_landed=False`` skips building the
    as-delivered :class:`LandedRow`s, for a caller that only types and will
    not commit (the layer walk over a 260 MB file).

    Variants are probed in order, each with its own read settings, because
    finding the header can itself depend on the variant: the ABC extract has a
    ``sep=,`` line and a report title above its header as a CSV, and one title
    row as a workbook. Probing stops at the first variant that both reads and
    binds, so the common case costs one open.

    A non-seekable stream can only be probed once, so it is read with the first
    variant's settings. Zip members and local files are seekable; this only
    constrains a future network source, and it fails loudly rather than
    silently mis-reading.

    Returns a :class:`FileLoad` whatever happens to the data. Raises
    ``ValueError`` only for a stream with no ``filename``.
    """
    if filename is None:
        if not isinstance(source, Path):
            raise ValueError("filename is required when loading from a stream")
        filename = source.name

    attempts: list[tuple] = []  # (binding, rows) for each variant that read and bound
    last_error: str = ""  # why the most recent variant was rejected, for UNRECOGNISED

    for variant in spec.variants:
        # Each probe re-reads from the top with this variant's read settings
        # (preamble, header marker, encoding, delimiter).
        if attempts and not _rewind(source):
            break  # unseekable stream: one attempt only
        try:
            header, rows = open_source(source, variant.read, sheet=sheet)
        except (StopIteration, UnicodeDecodeError, KeyError) as exc:
            # StopIteration: file shorter than the preamble. UnicodeDecodeError:
            # wrong encoding for this variant. KeyError: the named sheet is not
            # in this workbook. All mean "not this variant", not "bad file".
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        try:
            # bind() runs every variant's detector against the header, not
            # just this one's - so the header read with variant A's settings
            # may legitimately bind to variant B.
            binding = bind(spec, HeaderIndex(header))
        except NoVariantMatchedError as exc:
            last_error = str(exc)
            continue
        attempts.append((binding, rows, tuple(header)))
        if binding.variant.name == variant.name:
            break  # read settings and bound variant agree
        # Otherwise keep probing: the header bound, but to a variant whose own
        # read settings have not been tried yet. The last successful attempt
        # wins below, so a later exact agreement replaces this cross-match.

    if not attempts:
        return FileLoad(
            spec.key, filename, LoadOutcome.UNRECOGNISED, detail=last_error, sheet=sheet
        )

    binding, rows, header = attempts[-1]
    load = _load_stream(
        spec,
        binding,
        rows,
        filename=filename,
        sheet=sheet,
        ndc_directory=ndc_directory,
        row_hash_fields=None,
        header=header,
        keep_landed=keep_landed,
    )
    # The file hash is the identity in meta.source_file. It needs the bytes on
    # disk, so a stream gets no hash here; load_workbook computes one hash for
    # all its sheets and passes compute_file_hash=False to avoid repeating it.
    if compute_file_hash and isinstance(source, Path):
        load.file_hash = file_sha256(source)
    return load


def load_workbook(
    path: Path,
    spec: SourceSpec,
    *,
    ndc_directory: NdcDirectory | None = None,
    keep_landed: bool = True,
) -> list[FileLoad]:
    """Load every worksheet of a workbook that any variant selects.

    One workbook can hold several logical datasets - the Master COGS List has a
    consolidated sheet and five per-wholesaler tabs, which bind to different
    variants of the same spec. Each sheet is its own load with its own row
    accounting, so one bad tab does not obscure the rest.

    Returns one :class:`FileLoad` per selected sheet, in workbook order; an
    empty list when no variant selects any sheet.
    """
    # Union of the sheets every variant selects, de-duplicated, in workbook
    # order. Two variants may select the same tab; it is loaded once.
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
            keep_landed=keep_landed,
        )
        loads.append(load)
    # One hash for the physical file, shared by every sheet's load. The unique
    # constraint on meta.source_file is (tenant, hash, sheet), so each tab is
    # still a distinct source_file row.
    if loads:
        digest = file_sha256(path)
        for load in loads:
            load.file_hash = digest
    return loads
