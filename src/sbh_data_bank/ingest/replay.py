"""Typing a file again from its landed rows, without the file.

The landed layer keeps every cell as delivered text, by spec target, plus
the cells no spec column claimed under their vendor header, and
``meta.source_file.header_json`` keeps the header row with the position
each target bound to. That is enough to put the delivered row stream back
together and run it through the same binder and typer the file went
through, which is what "a wrong key or format is a re-run in the
database, never a re-read of files" (``docs/architecture-and-plan.md``,
"Storage layers") requires.

Two things the rebuilt stream cannot carry, both by design:

- A prohibited column's cell. It was never read; its position gets ``None``.
  ``type_row`` skips the column before indexing it, so nothing differs.
- The native type of a workbook cell. openpyxl hands the reader
  ``datetime``, ``int``, ``float`` and ``bool`` objects, and the landed
  layer holds ``str()`` of each. The number and boolean parsers read the
  text back to the same value; a date column does not, because
  ``parse_date`` takes a ``datetime`` as-is but holds text to the spec's
  declared formats, and ``str(datetime)`` is none of them. So for a
  worksheet load the one rendering ``str`` gives a ``datetime``,
  ``YYYY-MM-DD HH:MM:SS[.ffffff]``, is turned back into one. Nothing else
  is reinterpreted, and a CSV cell is never touched: text that happens to
  look like a timestamp in a CSV was text when the file loaded and is
  text again here.

How this fits
-------------
``commit.retype`` reads the landed rows and header of each loaded file
from a store and calls :func:`load_from_landed`; what comes back is a
:class:`~sbh_data_bank.ingest.load.FileLoad` like any other, compared
against the typed and quarantine rows the original load wrote.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sbh_data_bank.ingest.load import FileLoad, LoadOutcome, _load_stream
from sbh_data_bank.ingest.reader import RawRow
from sbh_data_bank.registry.binding import NoVariantMatchedError, bind
from sbh_data_bank.registry.headers import HeaderIndex
from sbh_data_bank.registry.spec import SourceSpec

__all__ = ["LandedLine", "header_from_json", "load_from_landed", "rebuild_rows"]

#: ``str(datetime)``: what a workbook's date cell became in the landed layer.
_WORKBOOK_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d{1,6})?$")


@dataclass(frozen=True, slots=True)
class LandedLine:
    """One ``landed.*`` row as a store returns it: the cells by target and
    the unclaimed cells by vendor header (``extra_cells`` decoded)."""

    line_number: int
    cells: dict[str, str | None]
    extra_cells: dict[str, str]


def header_from_json(header_json: str) -> tuple[list[str], dict[int, str]]:
    """The header row and the ``position -> target`` map the commit engine
    recorded on ``meta.source_file``."""
    entries = json.loads(header_json)
    header = [str(e["header"]) for e in sorted(entries, key=lambda e: e["position"])]
    targets = {int(e["position"]): e["target"] for e in entries if e.get("target")}
    return header, targets


def _native(value: str | None, *, workbook: bool) -> Any:
    """Undo the one ``str()`` that changes how a cell types (module docstring)."""
    if workbook and value is not None and _WORKBOOK_DATETIME.match(value):
        return datetime.fromisoformat(value)
    return value


def rebuild_rows(
    header: list[str],
    targets: dict[int, str],
    lines: Iterable[LandedLine],
    *,
    sheet: str | None,
) -> Iterator[RawRow]:
    """The delivered row stream, one :class:`RawRow` per landed line, cells
    in header order: a bound position reads its target's cell, an unbound
    position reads the unclaimed cell under its header, and a position with
    neither (a prohibited column, an empty junk column) is ``None``."""
    workbook = sheet is not None
    for line in lines:
        values = []
        for position, name in enumerate(header):
            target = targets.get(position)
            cell = line.cells.get(target) if target is not None else line.extra_cells.get(name)
            values.append(_native(cell, workbook=workbook))
        yield RawRow(line_number=line.line_number, values=tuple(values), sheet=sheet)


def load_from_landed(
    spec: SourceSpec,
    header_json: str,
    lines: Iterable[LandedLine],
    *,
    filename: str,
    sheet: str | None,
    file_hash: str | None = None,
) -> FileLoad:
    """Type a loaded file again from its landed rows under ``spec`` as it is
    now. Returns a :class:`FileLoad` exactly as ``load_file`` would for the
    file on disk, landed rows included, so the two can be compared line by
    line; ``UNRECOGNISED`` if no variant of the current spec binds the
    recorded header, which is what a spec edit that dropped a required
    column would do."""
    header, targets = header_from_json(header_json)
    try:
        binding = bind(spec, HeaderIndex(list(header)))
    except NoVariantMatchedError as exc:
        return FileLoad(spec.key, filename, LoadOutcome.UNRECOGNISED, detail=str(exc), sheet=sheet)
    load = _load_stream(
        spec,
        binding,
        rebuild_rows(header, targets, lines, sheet=sheet),
        filename=filename,
        sheet=sheet,
        ndc_directory=None,
        row_hash_fields=None,
        header=tuple(header),
        keep_landed=True,
    )
    load.file_hash = file_hash
    return load
