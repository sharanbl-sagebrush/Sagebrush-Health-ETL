"""Reading a vendor file into typed rows.

Streaming throughout. Curve Monthly Services is 260 MB and 230,192 rows, Curve
Appointments 143 MB, and the Monthly Payments set is 1.29 million rows across
40 files - none of which the previous pipeline ever loaded, and all of which it
would have processed a row at a time through ``df.iterrows()``. Nothing here
holds a whole file in memory.

Three invariants, taken from the definition of done:

**One bad row never fails a file.** A row whose values will not parse goes to
quarantine with a reason code and its raw payload. The file keeps loading.

**Every row is accounted for.** ``rows_read == rows_ok + rows_quarantined``,
asserted at the end of every load. A file that cannot account for every row it
read is a failure, not a partial success. This is the check that would have
caught the 227,284 payment rows the previous currency parser silently nulled.

**Prohibited values never materialise.** A ``PROHIBITED`` column is located so
that drift and column-count checks still see it, and then never read out of the
row. Its value does not reach the typed row, the quarantine payload, or a log
line - including when some *other* column on the same row fails to parse.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO

from sbh_data_bank.registry.binding import Binding
from sbh_data_bank.registry.spec import FieldType, OnError, PiiClass, ReadSpec
from sbh_data_bank.transforms.ndc import NdcDirectory, normalize_ndc
from sbh_data_bank.transforms.values import (
    ParseResult,
    parse_bool,
    parse_currency,
    parse_date,
    parse_decimal,
    parse_int,
    parse_text,
)

__all__ = [
    "CellError",
    "QuarantinedRow",
    "RawRow",
    "ReadCounters",
    "TypedRow",
    "open_source",
    "resolve_sheet",
    "type_row",
]

_EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls"}


@dataclass(frozen=True, slots=True)
class RawRow:
    """One row as it came out of the file, before typing."""

    line_number: int
    """1-based position in the file, header included, so an operator can find
    the row in the original."""
    values: tuple[Any, ...]
    """Cells in file order. Indexed by the positions in the binding."""
    sheet: str | None = None


@dataclass(frozen=True, slots=True)
class CellError:
    """Why one cell could not be used."""

    column: str
    reason: str
    raw: str
    """The offending value. Never populated for a PROHIBITED column - see
    :func:`type_row`."""


@dataclass(frozen=True, slots=True)
class TypedRow:
    """A row that parsed cleanly."""

    line_number: int
    values: dict[str, Any]
    sheet: str | None = None


@dataclass(frozen=True, slots=True)
class QuarantinedRow:
    """A row that did not parse, kept whole so it can be fixed and replayed."""

    line_number: int
    errors: tuple[CellError, ...]
    payload: dict[str, str]
    """Raw values by target name, for the columns the spec knows. Excludes every
    PROHIBITED column."""
    sheet: str | None = None

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(sorted({e.reason for e in self.errors}))


def _open_text(source: Path | BinaryIO, read: ReadSpec) -> io.TextIOBase:
    if isinstance(source, Path):
        return source.open("r", encoding=read.encoding, errors="strict", newline="")
    return io.TextIOWrapper(source, encoding=read.encoding, errors="strict", newline="")


def _trim_trailing_empty(cells: list[Any]) -> list[str]:
    out = [("" if c is None else str(c)) for c in cells]
    while out and not out[-1].strip():
        out.pop()
    return out


def _csv_header_and_rows(
    source: Path | BinaryIO, read: ReadSpec
) -> tuple[list[str], Iterator[RawRow]]:
    handle = _open_text(source, read)
    for _ in range(read.preamble_lines):
        handle.readline()
    reader = csv.reader(handle, delimiter=read.delimiter)
    header = _trim_trailing_empty(next(reader, []))
    header_line = read.preamble_lines + 1

    def rows() -> Iterator[RawRow]:
        try:
            for offset, cells in enumerate(reader, start=1):
                yield RawRow(line_number=header_line + offset, values=tuple(cells))
        finally:
            if isinstance(source, Path):
                handle.close()

    return header, rows()


def _excel_header_and_rows(
    path: Path, read: ReadSpec, sheet: str
) -> tuple[list[str], Iterator[RawRow]]:
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    worksheet = workbook[sheet]
    stream = worksheet.iter_rows(values_only=True)
    for _ in range(read.preamble_lines):
        next(stream, None)
    header = _trim_trailing_empty(list(next(stream, ())))
    header_line = read.preamble_lines + 1

    def rows() -> Iterator[RawRow]:
        try:
            for offset, cells in enumerate(stream, start=1):
                yield RawRow(line_number=header_line + offset, values=tuple(cells), sheet=sheet)
        finally:
            workbook.close()

    return header, rows()


def resolve_sheet(path: Path, read: ReadSpec) -> list[str]:
    """Worksheet names this read spec selects, in workbook order."""
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return [n for n in workbook.sheetnames if read.selects_sheet(n)]
    finally:
        workbook.close()


def _is_blank(row: RawRow) -> bool:
    return all(v is None or str(v).strip() == "" for v in row.values)


def open_source(
    source: Path | BinaryIO,
    read: ReadSpec,
    *,
    sheet: str | None = None,
) -> tuple[list[str], Iterator[RawRow]]:
    """Return the header row and a stream of data rows, in a single pass.

    One pass matters for a 260 MB CSV and for reading a member of a zip, where
    the handle cannot be rewound to read the header separately.

    Trailing blank rows are dropped when the spec asks - Cardinal detail exports
    end with one - but only *trailing* ones. A blank row in the middle of a file
    is a real anomaly and is passed through so it quarantines and somebody looks
    at it.
    """
    if isinstance(source, Path) and source.suffix.lower() in _EXCEL_SUFFIXES:
        if sheet is None:
            raise ValueError("reading a workbook requires a sheet name")
        header, rows = _excel_header_and_rows(source, read, sheet)
    else:
        header, rows = _csv_header_and_rows(source, read)

    if not read.skip_trailing_blank_rows:
        return header, rows

    def without_trailing_blanks() -> Iterator[RawRow]:
        # Blanks are held back rather than buffering the file: they are emitted
        # only once a non-blank row proves they were interior, and discarded at
        # end of file.
        pending: list[RawRow] = []
        for row in rows:
            if _is_blank(row):
                pending.append(row)
                continue
            if pending:
                yield from pending
                pending.clear()
            yield row

    return header, without_trailing_blanks()


def _parse_cell(value: Any, column, ndc_directory: NdcDirectory | None) -> ParseResult[Any]:
    kind = column.type
    if kind is FieldType.TEXT:
        return parse_text(value, max_length=column.max_length)
    if kind is FieldType.INTEGER:
        return parse_int(value)
    if kind is FieldType.DECIMAL:
        return parse_decimal(value)
    if kind is FieldType.CURRENCY:
        return parse_currency(value)
    if kind is FieldType.BOOLEAN:
        return parse_bool(value)
    if kind is FieldType.DATE:
        return parse_date(value, column.date_formats)
    if kind is FieldType.NDC:
        resolution = normalize_ndc(value, ndc_directory)
        if resolution.ok:
            return ParseResult(resolution.raw, resolution.ndc11)
        if not resolution.raw:
            return ParseResult.empty("")
        return ParseResult.failed(resolution.raw, resolution.quarantine_reason or "NDC_INVALID")
    raise AssertionError(f"unhandled field type {kind}")


def type_row(
    row: RawRow,
    binding: Binding,
    *,
    ndc_directory: NdcDirectory | None = None,
    injected: dict[str, Any] | None = None,
) -> TypedRow | QuarantinedRow:
    """Parse one raw row against its binding.

    ``injected`` supplies values for reader-injected columns - the worksheet
    name today - and for the spec's constants.

    A PROHIBITED column is never read. Its position is known, so drift checks
    work, but the value is not fetched, not typed, not put in the payload and
    not mentioned in an error. That holds even when a different column on the
    same row fails and the whole row is quarantined, which is the case that
    would otherwise leak an SSN into a quarantine table nobody thought of as
    sensitive.
    """
    injected = injected or {}
    values: dict[str, Any] = {}
    errors: list[CellError] = []
    payload: dict[str, str] = {}

    if _is_blank(row):
        # A wholly empty row reaching this point is interior - trailing ones are
        # dropped upstream. Reporting it as one BLANK_ROW is far more useful to
        # an operator than a dozen separate missing-value errors, and it names
        # the actual anomaly: a gap, a stray newline, or a totals footer.
        return QuarantinedRow(
            line_number=row.line_number,
            errors=(CellError(column="*", reason="BLANK_ROW", raw=""),),
            payload={},
            sheet=row.sheet,
        )

    for column_binding in binding.columns:
        column = column_binding.column

        if column.pii is PiiClass.PROHIBITED:
            continue

        if column.is_injected:
            values[column.target] = injected.get(column.target)
            continue

        if column_binding.match is None:
            values[column.target] = None
            continue

        position = column_binding.match.position
        raw = row.values[position] if position < len(row.values) else None
        payload[column.target] = "" if raw is None else str(raw)

        result = _parse_cell(raw, column, ndc_directory)
        if result.ok:
            if column.required and result.value is None:
                errors.append(
                    CellError(column=column.target, reason="MISSING_REQUIRED_VALUE", raw="")
                )
                continue
            values[column.target] = result.value
            continue

        if column.on_error is OnError.NULL:
            values[column.target] = None
            continue
        errors.append(
            CellError(column=column.target, reason=result.reason or "UNPARSEABLE", raw=result.raw)
        )

    if errors:
        return QuarantinedRow(
            line_number=row.line_number,
            errors=tuple(errors),
            payload=payload,
            sheet=row.sheet,
        )

    for name, constant in binding.spec.constants.items():
        values[name] = constant
    return TypedRow(line_number=row.line_number, values=values, sheet=row.sheet)


def sum_column(rows: Iterator[TypedRow], column: str) -> Decimal:
    """Total one numeric column. Used for control totals."""
    total = Decimal(0)
    for row in rows:
        value = row.values.get(column)
        if isinstance(value, Decimal):
            total += value
    return total


@dataclass(slots=True)
class ReadCounters:
    """Row accounting for one file.

    Every row read must end up in exactly one bucket. A load that cannot say
    where each row went is not a partial success, it is a failure.
    """

    rows_read: int = 0
    rows_ok: int = 0
    rows_quarantined: int = 0
    control_totals: dict[str, Decimal] = field(default_factory=dict)

    @property
    def balances(self) -> bool:
        return self.rows_read == self.rows_ok + self.rows_quarantined

    def imbalance(self) -> str | None:
        if self.balances:
            return None
        return (
            f"row accounting does not balance: read {self.rows_read}, "
            f"ok {self.rows_ok} + quarantined {self.rows_quarantined} = "
            f"{self.rows_ok + self.rows_quarantined}"
        )
