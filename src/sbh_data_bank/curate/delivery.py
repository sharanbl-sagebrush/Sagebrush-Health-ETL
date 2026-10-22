"""What is known about a delivery before any of its rows are compared.

Three facts, each decided in ``docs/decision-record.md``:

As-of (D5)
    When the vendor extracted the file. It orders deliveries; arrival time
    never does. Resolved the same way for every source: the dated upload
    folder first, then the spec's override for backlog files, then the
    workbook's creation stamp, the end date in the filename, and finally a
    date the operator supplies. A file with none is refused, not guessed.

Coverage (D2)
    The date window the delivery claims to cover, from the filename, from the
    data, or "everything up to the as-of" for a snapshot, plus per-source
    offsets. The vanish outcome retires a current row only when a newer
    delivery covers its date and no longer carries it, so the window is a
    statement the spec makes, never an inference.

The as-of floor (D4)
    A delivery cannot have been extracted before its own data. Checked over
    the event date only, with a one-day tolerance, because Athena and Curve
    files carry bookings for the day after the pull.

How this fits
-------------
``scripts/layer_walk.py`` calls :func:`resolve_as_of` and
:func:`resolve_coverage` for every real file and feeds the results to
``curate.rule``. The commit engine, when built, records them on
``meta.source_file`` (``as_of``, ``as_of_source``, ``coverage_from``,
``coverage_to``). Nothing here reads a cell value; it reads names, folder
names, workbook metadata and typed dates.
"""

from __future__ import annotations

import re
import zipfile
from calendar import monthrange
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from sbh_data_bank.registry.spec import AsOfSource, CoverageSource, SourceSpec

__all__ = [
    "AS_OF_TOLERANCE_DAYS",
    "AsOf",
    "AsOfUnavailableError",
    "Coverage",
    "as_of_floor_violation",
    "filename_window",
    "folder_date",
    "resolve_as_of",
    "resolve_coverage",
    "xlsx_created",
    "xlsx_modified",
]

#: Days a delivery's latest event date may exceed its as-of (decision D4).
AS_OF_TOLERANCE_DAYS = 1


class AsOfUnavailableError(ValueError):
    """No as-of date could be established for a delivery.

    Raised rather than guessed: an unordered delivery would be applied in
    arrival order, which is the behaviour the as-of rule exists to prevent.
    The message says what was tried so the operator knows what to supply.
    """


@dataclass(frozen=True, slots=True)
class AsOf:
    """A delivery's as-of date and where it came from."""

    value: date
    source: AsOfSource


@dataclass(frozen=True, slots=True)
class Coverage:
    """The date window a delivery claims to cover, inclusive at both ends.

    ``start`` of ``None`` means "from the beginning of time": a snapshot, or
    a filename like ``as of 03062026`` that states only an end. ``end`` is
    always known once a window is resolved at all.

    Inclusive rather than the half-open ``core.temporal.DateRange`` because
    this is how filenames state it (``01012026-04052026`` covers 5 April) and
    how the offsets are reasoned about ("Cardinal ends one day early").
    """

    start: date | None
    end: date
    source: CoverageSource

    def covers(self, day: date) -> bool:
        """Whether ``day`` falls inside the window."""
        return (self.start is None or self.start <= day) and day <= self.end

    def __str__(self) -> str:
        return f"{self.start or 'open'} to {self.end} ({self.source.value})"


# --------------------------------------------------------------------------
# Dates in names
# --------------------------------------------------------------------------

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}


def _mdy(s: str) -> date | None:
    """``MMDDYYYY`` to a date, or ``None`` when the digits are not a date."""
    try:
        return date(int(s[4:8]), int(s[0:2]), int(s[2:4]))
    except ValueError:
        return None


def filename_window(name: str) -> tuple[date | None, date | None]:
    """The date range a filename claims, or ``(None, None)``.

    Shapes seen on real files, all handled: ``01012026-04052026``,
    ``Jan012025-Feb042026``, ``Jan012025_Feb042026``, ``as of 03062026``,
    ``upto 07212026``, a lone ``Feb042026`` (a snapshot's extract date, the
    end of its window; added 2026-10-21 for Athena eligibility), ``2025-06``
    (a month). The end date is also the as-of fallback for CSV files, which
    carry no stamp of their own.
    """
    m = re.search(r"(\d{8})-(\d{8})", name)
    if m:
        start, end = _mdy(m.group(1)), _mdy(m.group(2))
        if start and end:
            return start, end
    m = re.search(r"([A-Za-z]{3})(\d{2})(\d{4})[-_]([A-Za-z]{3})(\d{2})(\d{4})", name)
    if m and m.group(1).lower() in _MONTHS and m.group(4).lower() in _MONTHS:
        try:
            a = date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
            b = date(int(m.group(6)), _MONTHS[m.group(4).lower()], int(m.group(5)))
            return a, b
        except ValueError:
            pass
    m = re.search(r"(?:as of|upto)\s*(\d{8})", name, re.IGNORECASE)
    if m:
        return None, _mdy(m.group(1))
    m = re.search(r"(?<![A-Za-z])([A-Za-z]{3})(\d{2})(\d{4})(?!\d)", name)
    if m and m.group(1).lower() in _MONTHS:
        try:
            return None, date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
        except ValueError:
            pass
    m = re.search(r"(\d{4})-(\d{2})(?!\d)", name)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            return date(y, mo, 1), date(y, mo, monthrange(y, mo)[1])
    return None, None


_FOLDER_DATE = re.compile(r"(?:^|\s)(\d{4}-\d{2}-\d{2}|\d{8})$")


def folder_date(path: Path, *, root: Path | None = None) -> date | None:
    """The upload date a dated folder names, or ``None``.

    Decision D5: every Sunday upload goes into a folder named for that date,
    and the folder date is the as-of for every file in it. A folder qualifies
    when its name is a date, or ends in one after a space, in ``YYYY-MM-DD``
    or ``MMDDYYYY`` form (``2026-10-18``, ``Sagebrush ETL files 03062026``).
    A date range (``Jan012026 - Apr052026``) is not a date and does not
    qualify. The nearest qualifying ancestor wins; the search stops at
    ``root`` so a dated folder above the source tree cannot leak in.
    """
    for folder in path.parents:
        if root is not None and folder == root:
            break
        m = _FOLDER_DATE.search(folder.name.strip())
        if not m:
            continue
        token = m.group(1)
        if "-" in token:
            try:
                return date.fromisoformat(token)
            except ValueError:
                continue
        found = _mdy(token)
        if found is not None:
            return found
    return None


# --------------------------------------------------------------------------
# Workbook stamps
# --------------------------------------------------------------------------


def _core_stamp(path: Path, element: str) -> date | None:
    """A date from ``docProps/core.xml`` inside an xlsx, or ``None``.

    Written by the vendor portal that generated the export (SAP, Apache POI,
    Kendo on the real files), so the creation stamp is the extraction date.
    Absent on two of the 32 real workbooks, and meaningless for CSV, which
    has no such part.
    """
    try:
        with zipfile.ZipFile(path) as z:
            if "docProps/core.xml" not in z.namelist():
                return None
            core = z.read("docProps/core.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, OSError):
        return None
    m = re.search(rf"<dcterms:{element}[^>]*>(\d{{4}}-\d{{2}}-\d{{2}})", core)
    return datetime.strptime(m.group(1), "%Y-%m-%d").date() if m else None


def xlsx_created(path: Path) -> date | None:
    """The workbook's internal creation stamp, if any."""
    return _core_stamp(path, "created")


def xlsx_modified(path: Path) -> date | None:
    """The workbook's internal last-modified stamp, if any."""
    return _core_stamp(path, "modified")


# --------------------------------------------------------------------------
# As-of
# --------------------------------------------------------------------------


def _from_source(
    source: AsOfSource, path: Path, filename: str, operator_date: date | None
) -> date | None:
    """Read one as-of source; ``None`` when that source has nothing."""
    if source is AsOfSource.XLSX_CREATED:
        return xlsx_created(path) if path.suffix.lower() == ".xlsx" else None
    if source is AsOfSource.XLSX_MODIFIED:
        return xlsx_modified(path) if path.suffix.lower() == ".xlsx" else None
    if source is AsOfSource.FILENAME:
        return filename_window(filename)[1]
    if source is AsOfSource.OPERATOR:
        return operator_date
    raise AssertionError(f"unhandled as-of source {source}")


def resolve_as_of(
    path: Path,
    spec: SourceSpec,
    *,
    filename: str | None = None,
    operator_date: date | None = None,
    root: Path | None = None,
) -> AsOf:
    """The as-of date of one delivery, and where it came from.

    The chain, decision D5:

    1. A dated upload folder above the file (:func:`folder_date`). The rule
       going forward, for every source.
    2. The spec's ``as_of_source`` override, if it has one. Exists for the
       backlog files, which have no dated folder: ABC's xlsx stamp is an
       Excel re-save so it uses the filename; the Master COGS List's creation
       stamp never moves so it uses the modified stamp. An override that
       yields nothing is refused rather than falling through, because the
       spec said where to look.
    3. The default chain: xlsx creation stamp, filename end date, then the
       operator-supplied date.

    ``filename`` defaults to ``path.name``; pass it for a zip member, whose
    name carries the date range while the archive's does not. ``root`` bounds
    the folder search. Raises :class:`AsOfUnavailableError` when nothing
    yields a date.
    """
    filename = filename or path.name
    found = folder_date(path, root=root)
    if found is not None:
        return AsOf(found, AsOfSource.FOLDER)
    if spec.as_of_source is not None:
        found = _from_source(spec.as_of_source, path, filename, operator_date)
        if found is None:
            raise AsOfUnavailableError(
                f"{filename}: spec {spec.key} takes as-of from {spec.as_of_source.value}, "
                "which this file does not carry; supply it or correct the spec"
            )
        return AsOf(found, spec.as_of_source)
    for source in (AsOfSource.XLSX_CREATED, AsOfSource.FILENAME, AsOfSource.OPERATOR):
        found = _from_source(source, path, filename, operator_date)
        if found is not None:
            return AsOf(found, source)
    raise AsOfUnavailableError(
        f"{filename}: no as-of date - not in a dated folder, no xlsx creation stamp, "
        "no date range in the name and no operator date supplied"
    )


# --------------------------------------------------------------------------
# Coverage and the as-of floor
# --------------------------------------------------------------------------


def _as_day(value: object) -> date | None:
    """A date from a typed event-date value; ``None`` for null or non-dates."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def resolve_coverage(
    spec: SourceSpec,
    *,
    filename: str,
    as_of: date,
    coverage_dates: Iterable[object] = (),
) -> Coverage | None:
    """The date window a delivery covers, or ``None`` when it cannot be known.

    ``coverage_dates`` are the typed values of the spec's coverage column
    (``SourceSpec.coverage_target``) over the delivery's rows; only the
    ``data`` source reads them. The spec's offsets are applied last, to
    whichever window was found. ``None`` means the vanish outcome cannot run
    for this delivery; callers decide whether that refuses the file.
    """
    if spec.coverage is CoverageSource.AS_OF:
        start: date | None = None
        end: date | None = as_of
    elif spec.coverage is CoverageSource.FILENAME:
        start, end = filename_window(filename)
    else:
        days = [d for d in (_as_day(v) for v in coverage_dates) if d is not None]
        start, end = (min(days), max(days)) if days else (None, None)
    if end is None:
        return None
    if start is not None:
        start += timedelta(days=spec.coverage_start_offset_days)
    end += timedelta(days=spec.coverage_end_offset_days)
    if start is not None and start > end:
        return None
    return Coverage(start, end, spec.coverage)


def as_of_floor_violation(
    as_of: date,
    event_dates: Iterable[object],
    *,
    tolerance_days: int = AS_OF_TOLERANCE_DAYS,
) -> str | None:
    """Why a delivery's as-of precedes its own data, or ``None`` when it does not.

    A vendor cannot have extracted a file before the events in it happened,
    so an as-of earlier than the latest event date means the as-of is wrong
    (a stale operator date, a mis-dated folder) and the delivery would be
    ordered wrongly. The tolerance covers next-day bookings (decision D4).
    """
    latest: date | None = None
    for value in event_dates:
        day = _as_day(value)
        if day is not None and (latest is None or day > latest):
            latest = day
    if latest is None or latest <= as_of + timedelta(days=tolerance_days):
        return None
    return (
        f"as-of {as_of} precedes the latest event date {latest} by "
        f"{(latest - as_of).days} days (tolerance {tolerance_days})"
    )
