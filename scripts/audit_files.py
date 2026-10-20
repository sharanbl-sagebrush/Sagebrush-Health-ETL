"""Structural audit of every vendor file, against what the ETL actually handles.

Answers one question per file: is there anything in its shape that the reader
and the spec model do not cope with? It looks at the things that break loads
quietly rather than loudly -

    preamble        lines above the header (a ``sep=`` declaration, a report title)
    header          which row it really is, duplicates, blank names, junk names
    footer          a trailing totals line, which lands as a phantom record if missed
    ragged rows     data rows with more or fewer fields than the header
    embedded text   newlines and delimiters inside quoted fields
    encoding        BOM, non-UTF-8 bytes
    workbook        sheet names, merged header cells, hidden sheets

Reads the head and tail of each file only, so a 260 MB extract costs the same
as a small one. Values are examined but never printed: output is structure,
counts and column names.

    python scripts/audit_files.py [root] [--full]

``--full`` scans every data row for raggedness instead of a sample.

How to run
    ``python scripts/audit_files.py``                 audit the default root
    ``python scripts/audit_files.py D:\\drops``        audit another folder
    ``python scripts/audit_files.py --full``          scan every row, not a sample

Inputs
    ``root``            optional positional path; defaults to ``DEFAULT_ROOT``.
    ``SBH_SOURCE_ROOT`` environment variable; the parent of ``Original Files``.
                        The hard-coded fallback is one machine's OneDrive path.
    No database and no credentials are needed. Nothing is written to disk.

Outputs
    Plain text on stdout: one block per file (or per worksheet of a workbook)
    listing kind, size, encoding, header row index, column count, the sampled
    row count, up to three preamble lines, and every finding. A closing summary
    counts the audits and repeats every GAP finding. The exit code is always 0;
    this is a report for a human, not a gate.

Finding levels
    INFO      a fact worth knowing that needs no change
    HANDLED   a quirk the reader or spec model already copes with, with the
              spec setting that handles it named in the message
    GAP       something the pipeline does not cope with, or could not audit

What it does NOT do
    It does not bind files to specs (``scripts/bind_real_files.py`` does that),
    does not check natural-key uniqueness, and does not load anything. It also
    never prints a cell value: the extracts are PHI, and the output is designed
    to be pasted into a document or a ticket.

Why head and tail only
    The largest extract (Curve Monthly Services) is 260 MB. Reading the first
    ``SAMPLE_ROWS`` rows finds the preamble, header and raggedness pattern, and
    reading the last ``TAIL_BYTES`` finds a footer. Everything the audit looks
    for is visible in those two places, so the cost is flat per file.

Where it fits
    This script is the evidence behind ``docs/file-audit.md``. The two real
    changes that document records - the McKesson Purchasing Detail totals
    footer and ABC's variable-length preamble - were found by running it. It is
    the thing to re-run when a vendor changes an export, before touching a
    spec.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.registry.headers import is_junk_header, normalize_header  # noqa: E402

#: Where the vendor extracts live. They are PHI and stay outside the
#: repository, so the location is per-machine: set SBH_SOURCE_ROOT, or pass
#: a path argument. The fallback is the layout this was first written against.
DEFAULT_ROOT = (
    Path(
        os.environ.get(
            "SBH_SOURCE_ROOT",
            r"C:\Users\SharanLokeshwar\OneDrive - Sagebrush Healthcare\Sagebrush ETL Files",
        )
    )
    / "Original Files"
)
#: File types the audit knows how to open. Zip archives are handled separately
#: (``audit_zip``) because Monthly Payments ships as 40 CSVs inside one zip.
DATA_SUFFIXES = {".csv", ".xlsx", ".xlsm", ".xls"}
#: How much of the end of a CSV to read when looking for a footer. 256 KB is
#: far more than any footer needs and still trivial against a 260 MB file.
TAIL_BYTES = 256 * 1024
#: How many data rows to sample from the top of each file unless ``--full``.
SAMPLE_ROWS = 5000


@dataclass
class Finding:
    """One structural fact worth a human's attention."""

    level: str  # INFO | HANDLED | GAP
    what: str

    def __str__(self) -> str:
        return f"[{self.level:<7}] {self.what}"


@dataclass
class FileAudit:
    """Everything the audit learned about one file, or one worksheet of one file.

    A workbook produces one ``FileAudit`` per sheet (``sheet`` set); a CSV
    produces one with ``sheet`` left ``None``. ``field_counts`` is a histogram
    of populated-field-count per sampled row and is what raggedness is read
    from. ``tail_rows`` keeps the last two sampled rows for reference only.
    """

    name: str
    kind: str
    sheet: str | None = None
    size_mb: float = 0.0
    encoding: str = ""
    preamble: list[str] = field(default_factory=list)
    header: list[str] = field(default_factory=list)
    header_row_index: int = 0
    data_rows_sampled: int = 0
    field_counts: Counter = field(default_factory=Counter)
    tail_rows: list[list[str]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, what: str) -> None:
        """Record one finding. ``level`` is INFO, HANDLED or GAP."""
        self.findings.append(Finding(level, what))

    @property
    def gaps(self) -> list[Finding]:
        """Only the findings that mean the pipeline cannot cope as it stands."""
        return [f for f in self.findings if f.level == "GAP"]


def _decode(raw: bytes) -> tuple[str, str]:
    """Decode a byte sample and report which encoding worked.

    Returns ``(text, label)``. The label goes into the report so a reader can
    see that, for example, Allscripts eRx ships with a UTF-8 byte-order mark
    (docs/file-audit.md) and nothing else in the estate does. Mojibake in an
    identifier is the failure this prevents: a wrongly decoded account number
    looks valid and simply never joins.
    """
    # A UTF-8 BOM is the one case that is unambiguous from the bytes alone.
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace"), "utf-8-sig (BOM)"
    # Otherwise try the strict decoders in order of how likely a vendor is to
    # have used them. cp1252 is what Windows exports produce; latin-1 accepts
    # every byte so it is the last resort.
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    # Unreachable in practice because latin-1 never raises, but kept so the
    # function always returns and the label says the decode was lossy.
    return raw.decode("latin-1", errors="replace"), "latin-1 (lossy)"


def _looks_like_header(cells: list[str]) -> bool:
    """Heuristic: is this row the column header rather than a title or data?

    Used to find the header without a spec, because the audit runs on files
    the specs may not describe yet. The loader itself never guesses this way;
    it uses the spec's ``preamble_lines`` or ``header_marker``.
    """
    populated = [c.strip() for c in cells if c.strip()]
    # A report title is one or two cells; a header is many.
    if len(populated) < 3:
        return False
    # Header names are unique; a data row can repeat values freely.
    if len(set(populated)) != len(populated):
        return False
    # A header is names, not measurements: mostly non-numeric.
    numeric = sum(1 for c in populated if c.replace(".", "").replace(",", "").isdigit())
    return numeric <= len(populated) * 0.3


def _audit_rows(
    audit: FileAudit,
    header: list[str],
    rows: list[list[str]],
    *,
    tail: list[list[str]] | None = None,
) -> None:
    """Assess header, raggedness and footer.

    ``rows`` is a contiguous sample from the top and is what raggedness is
    measured on. ``tail`` is a separate, possibly fragmentary read of the end of
    the file and is used only to look for a footer.
    """
    audit.header = header
    audit.data_rows_sampled = len(rows)
    width = len(header)

    # Build the populated-width histogram that raggedness is read from.
    for row in rows:
        # Trailing empty fields are Excel padding, not data. Compare on the
        # populated width so a used-range that extends past the data - as the
        # Master COGS List does, to column K - is not reported as raggedness.
        trimmed = list(row)
        while trimmed and not str(trimmed[-1]).strip():
            trimmed.pop()
        audit.field_counts[max(len(trimmed), 1) if trimmed else 0] += 1

    # header quality - the same normalization the binder uses, so a duplicate
    # found here is a duplicate the binder would refuse as ambiguous.
    normalized = [normalize_header(h) for h in header if not is_junk_header(h)]
    duplicates = [n for n, count in Counter(normalized).items() if count > 1]
    if duplicates:
        audit.add("GAP", f"duplicate header names after normalization: {duplicates}")
    junk = [h for h in header if is_junk_header(h)]
    if junk:
        audit.add("HANDLED", f"export-artifact columns ignored: {junk[:4]}")
    # An unnamed column is only a gap if it carries data the spec cannot name.
    # McKesson prefixes its exports with an unnamed row-number column, which is
    # an artifact and is excluded from matching by is_junk_header.
    blank_interior = [
        i for i, h in enumerate(header[:-1]) if not str(h).strip() and not is_junk_header(h)
    ]
    if blank_interior:
        audit.add("GAP", f"blank header name at interior positions {blank_interior[:5]}")
    unnamed = [i for i, h in enumerate(header) if is_junk_header(h)]
    if unnamed:
        audit.add(
            "INFO",
            f"unnamed/artifact columns at {unnamed} - excluded from matching; "
            "confirm they carry no data the spec needs",
        )

    # raggedness - rows whose populated width differs from the header's.
    # A row with MORE fields than the header is a gap: either the header is
    # wrong or an embedded delimiter split a cell, and either way a column
    # would bind to the wrong data. A row with FEWER is handled: the reader
    # fills the missing cells with null and a required column quarantines it.
    wrong = {n: c for n, c in audit.field_counts.items() if n != width}
    if wrong:
        short = {n: c for n, c in wrong.items() if n < width}
        long = {n: c for n, c in wrong.items() if n > width}
        if long:
            audit.add("GAP", f"rows with MORE fields than the header ({width}): {long}")
        if short:
            audit.add(
                "HANDLED",
                f"rows with fewer fields than the header ({width}): {short} - "
                "read as null, required columns then quarantine",
            )

    # footer - from the tail when there is one, else the sample.
    # A totals row has the shape "leading key cells blank, a few amount cells
    # populated". Only two files in the estate have one: Cardinal Invoice
    # Review (3 of 17 cells) and McKesson Purchasing Detail (11 of 45 cells).
    # Missing the McKesson one would have landed a phantom $20.4M purchase
    # (docs/file-audit.md). Skipping it silently would waste a free control
    # total, so the finding names the spec setting: footer: totals_row.
    footer_source = tail if tail else rows
    if footer_source:
        last = footer_source[-1]
        populated = [(i, v) for i, v in enumerate(last) if str(v).strip()]
        # "Sparse" means fewer than 40% of cells populated (minimum 3), and
        # "leading blank" means the first three cells - where keys live - are
        # empty. Both together are what a totals line looks like.
        if last and 0 < len(populated) < max(3, width * 0.4):
            leading_blank = all(not str(last[i]).strip() for i in range(min(3, len(last))))
            if leading_blank:
                audit.add(
                    "HANDLED" if len(populated) else "INFO",
                    f"trailing summary row: {len(populated)} of {width} cells populated "
                    f"at positions {[i for i, _ in populated]} - needs footer: totals_row",
                )
        # A completely empty last row is the common case (every Monthly
        # Payments file ends with one) and the reader drops it by default.
        if last and not any(str(v).strip() for v in last):
            audit.add("HANDLED", "trailing blank row - dropped by skip_trailing_blank_rows")

    # embedded newlines - a multi-line cell breaks a naive line-based reader
    # into two short rows. The csv module handles quoting correctly, so this is
    # HANDLED, but worth knowing: the Master COGS List has them in Location.
    multiline = sum(1 for row in rows for cell in row if "\n" in str(cell))
    if multiline:
        audit.add("HANDLED", f"{multiline} cells contain embedded newlines")


def audit_csv(path: Path, *, full: bool) -> FileAudit:
    """Audit one CSV: encoding, preamble, header, a row sample, and the tail.

    Reads the first 2 MB to find the header and sample rows, then (unless
    ``--full``) reads the last ``TAIL_BYTES`` separately to look for a footer.
    """
    audit = FileAudit(name=path.name, kind="csv", size_mb=round(path.stat().st_size / 1e6, 2))

    # Step 1: decode a 2 MB head. Enough for any preamble plus the header plus
    # thousands of rows, and bounded so a 260 MB file costs the same.
    head_raw = path.open("rb").read(2 * 1024 * 1024)
    text, encoding = _decode(head_raw)
    audit.encoding = encoding

    # Step 2: find the header within the first 30 lines. Lines above it are
    # the preamble. ABC's CSV form opens with "sep=," and a report title
    # (two lines); Athena opens with a "REPORT NAME :" title (one line).
    # Reading a title as the header binds every column to nothing, which is
    # the failure this check exists to surface.
    lines = text.splitlines()
    header_index = None
    for index, line in enumerate(lines[:30]):
        # Excel's "sep=," hint is recorded as preamble but never parsed as
        # cells; it would otherwise look like a one-column row.
        if line.lower().startswith("sep="):
            audit.preamble.append(line)
            continue
        cells = next(csv.reader([line]), [])
        if _looks_like_header(cells):
            header_index = index
            break
        # Preamble lines are truncated to 90 characters for the report. They
        # are titles, not data, so this is not a PHI exposure.
        if line.strip():
            audit.preamble.append(line[:90])
    if header_index is None:
        audit.add("GAP", "no header row found in the first 30 lines")
        return audit

    audit.header_row_index = header_index
    if audit.preamble:
        audit.add(
            "HANDLED",
            f"{len(audit.preamble)} preamble line(s) before the header "
            f"- needs preamble_lines: {header_index}",
        )

    # Step 3: re-open with the proper csv reader, skip the preamble, and take
    # the sample. ``newline=""`` is required for the csv module to handle
    # embedded newlines inside quoted cells correctly.
    with path.open("r", encoding=encoding.split()[0], errors="replace", newline="") as handle:
        for _ in range(header_index):
            handle.readline()
        reader = csv.reader(handle)
        header = next(reader, [])
        rows = []
        for index, row in enumerate(reader):
            if not full and index >= SAMPLE_ROWS:
                break
            rows.append(row)

    # Step 4: the tail. It is read separately so a footer is seen even when
    # only a sample of the head was taken. It is NOT merged into the
    # raggedness sample: seeking into the middle of the file lands mid-line,
    # and the resulting fragment counts as a short row that does not exist.
    # That produced phantom ragged-row findings on the Monthly Payments files
    # (rows of 38 and 7 fields against a 116-column header - docs/file-audit.md
    # "Findings that turned out not to be problems"). A full scan confirmed the
    # only non-conforming row in each file is a single trailing blank line.
    tail_rows: list[list[str]] = []
    if not full and path.stat().st_size > TAIL_BYTES:
        with path.open("rb") as handle:
            handle.seek(-TAIL_BYTES, io.SEEK_END)
            tail_text, _ = _decode(handle.read())
        # Drop the first two lines: the first is a fragment, and the second can
        # be too if the fragment contained an unbalanced quote.
        tail_rows = [r for r in csv.reader(tail_text.splitlines()[2:]) if r]

    # Only the last two tail rows matter: a footer is the final line, and the
    # one before it tells whether the file ends with a blank line.
    _audit_rows(audit, header, rows, tail=tail_rows[-2:] if tail_rows else None)
    audit.tail_rows = rows[-2:]
    return audit


def audit_workbook(path: Path, *, full: bool) -> list[FileAudit]:
    """Audit every worksheet of an Excel workbook, one ``FileAudit`` per sheet.

    Workbook-specific checks: hidden sheets and merged header cells, both of
    which only openpyxl's full (non-streaming) mode can see. Sheet names
    matter because they vary in case across deliveries (``Results``,
    ``sheet1``, ``Sheet1`` - docs/data-findings.md section 5).
    """
    import openpyxl

    out: list[FileAudit] = []
    # read_only=False is what exposes merged ranges and sheet visibility, but it
    # parses the whole workbook into memory - minutes, on a 25 MB extract. Only
    # small workbooks get the full inspection; large ones are streamed and the
    # skipped checks are reported rather than silently dropped.
    inspect_fully = path.stat().st_size <= 6_000_000
    workbook = openpyxl.load_workbook(path, read_only=not inspect_fully, data_only=True)
    try:
        for name in workbook.sheetnames:
            audit = FileAudit(
                name=path.name,
                kind="xlsx",
                sheet=name,
                size_mb=round(path.stat().st_size / 1e6, 2),
                encoding="n/a",
            )
            worksheet = workbook[name]
            if inspect_fully:
                # A hidden sheet is a gap because the spec's sheet selector
                # could pick it up, and a merged header cell is a gap because
                # it cannot be read as one flat row of names.
                if worksheet.sheet_state != "visible":
                    audit.add("GAP", f"sheet is {worksheet.sheet_state}, not visible")
                if worksheet.merged_cells.ranges:
                    audit.add(
                        "GAP",
                        f"{len(worksheet.merged_cells.ranges)} merged cell range(s) - "
                        "a merged header cannot be read as a flat row",
                    )
            else:
                audit.add("INFO", "large workbook: merged-cell and visibility checks skipped")

            # Pull the sampled rows into a grid of strings. Excel cells have
            # native types; the audit only cares about shape, so everything
            # becomes text and None becomes "".
            grid = []
            for index, row in enumerate(worksheet.iter_rows(values_only=True)):
                if not full and index > SAMPLE_ROWS:
                    break
                grid.append([("" if c is None else str(c)) for c in row])

            # Find the header the same way as for CSV: first header-looking
            # row in the top 30. ABC's workbook has one title row above it.
            header_index = None
            for index, row in enumerate(grid[:30]):
                if _looks_like_header(row):
                    header_index = index
                    break
                if any(c.strip() for c in row):
                    audit.preamble.append(" | ".join(c for c in row if c.strip())[:90])
            if header_index is None:
                audit.add("INFO" if not grid else "GAP", "no header row found")
                out.append(audit)
                continue

            audit.header_row_index = header_index
            if header_index:
                audit.add(
                    "HANDLED",
                    f"{header_index} preamble row(s) - needs preamble_lines: {header_index}",
                )
            # Trim trailing empty header cells. A worksheet's used range can
            # run past the data - the Master COGS List's header stops at G
            # but its used range runs to K - and without this trim every row
            # would look short (docs/file-audit.md). Workbooks get no separate
            # tail read: the whole sample is already in memory.
            header = grid[header_index]
            while header and not header[-1].strip():
                header.pop()
            _audit_rows(audit, header, grid[header_index + 1 :])
            out.append(audit)
    finally:
        workbook.close()
    return out


def audit_zip(path: Path, *, full: bool, limit: int = 3) -> list[FileAudit]:
    """Audit the CSV members of a zip archive.

    Exists for ``Monthly Payments.zip``, which holds 40 monthly files with one
    stable 116-column header. Auditing the first ``limit`` members and the last
    one is enough to confirm the shape is constant and to catch a change in the
    newest delivery. Members are read from a 4 MB head only; there is no
    separate tail read, so a footer inside a zip member would not be seen
    (none of the Monthly Payments files has one).
    """
    out: list[FileAudit] = []
    with zipfile.ZipFile(path) as archive:
        members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        # First `limit` members plus the last one, so the newest file is
        # always checked even when the archive grows.
        for member in members[:limit] + (members[-1:] if len(members) > limit else []):
            audit = FileAudit(name=f"{path.name}::{Path(member).name}", kind="csv-in-zip")
            with archive.open(member) as raw:
                text, encoding = _decode(raw.read(4 * 1024 * 1024))
            audit.encoding = encoding
            lines = text.splitlines()
            header_index = None
            for index, line in enumerate(lines[:30]):
                cells = next(csv.reader([line]), [])
                if _looks_like_header(cells):
                    header_index = index
                    break
                if line.strip():
                    audit.preamble.append(line[:90])
            if header_index is None:
                audit.add("GAP", "no header row found")
                out.append(audit)
                continue
            audit.header_row_index = header_index
            if audit.preamble:
                audit.add("HANDLED", f"{len(audit.preamble)} preamble line(s)")
            body = list(csv.reader(lines[header_index:]))
            _audit_rows(audit, body[0], body[1:])
            out.append(audit)
        if len(members) > limit:
            print(f"    (audited {limit + 1} of {len(members)} members)")
    return out


def main() -> int:
    """Walk the root, audit every data file, print the report. Always exits 0."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    audits: list[FileAudit] = []
    # Recursive walk in sorted order so the report is stable run to run.
    # "~$" files are Excel's lock files for a workbook somebody has open.
    for path in sorted(args.root.rglob("*")):
        if not path.is_file() or path.name.startswith("~$"):
            continue
        if path.suffix.lower() not in DATA_SUFFIXES and path.suffix.lower() != ".zip":
            continue
        print(f"auditing {path.name} ...", flush=True)
        try:
            if path.suffix.lower() == ".zip":
                audits.extend(audit_zip(path, full=args.full))
            elif path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
                audits.extend(audit_workbook(path, full=args.full))
            elif path.suffix.lower() == ".csv":
                audits.append(audit_csv(path, full=args.full))
        # One unreadable file becomes a GAP finding rather than a traceback,
        # so the other audits still run and the report is complete.
        except Exception as exc:  # noqa: BLE001 - an audit must never abort the run
            broken = FileAudit(name=path.name, kind=path.suffix)
            broken.add("GAP", f"could not audit: {type(exc).__name__}: {exc}")
            audits.append(broken)

    # Report: one block per audit, then a summary that repeats only the GAPs
    # so a reader can see at a glance whether anything needs a code change.
    for audit in audits:
        label = f"{audit.name}" + (f" [{audit.sheet}]" if audit.sheet else "")
        print(f"\n{label}")
        print(
            f"    {audit.kind} {audit.size_mb}MB enc={audit.encoding} "
            f"header_row={audit.header_row_index} cols={len(audit.header)} "
            f"sampled={audit.data_rows_sampled}"
        )
        if audit.preamble:
            for line in audit.preamble[:3]:
                print(f"    preamble: {line!r}")
        for finding in audit.findings:
            print(f"    {finding}")

    print("\n" + "=" * 78)
    gaps = [(a, f) for a in audits for f in a.gaps]
    print(f"{len(audits)} file/sheet audits, {len(gaps)} gap(s)")
    for audit, finding in gaps:
        label = f"{audit.name}" + (f" [{audit.sheet}]" if audit.sheet else "")
        print(f"  {label}: {finding.what}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
