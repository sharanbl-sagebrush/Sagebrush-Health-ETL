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
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.registry.headers import is_junk_header, normalize_header  # noqa: E402

DEFAULT_ROOT = Path(
    r"C:\Users\SharanLokeshwar\OneDrive - Sagebrush Healthcare\Sagebrush ETL Files\Original Files"
)
DATA_SUFFIXES = {".csv", ".xlsx", ".xlsm", ".xls"}
TAIL_BYTES = 256 * 1024
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
        self.findings.append(Finding(level, what))

    @property
    def gaps(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "GAP"]


def _decode(raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace"), "utf-8-sig (BOM)"
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1 (lossy)"


def _looks_like_header(cells: list[str]) -> bool:
    populated = [c.strip() for c in cells if c.strip()]
    if len(populated) < 3:
        return False
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

    for row in rows:
        # Trailing empty fields are Excel padding, not data. Compare on the
        # populated width so a used-range that extends past the data - as the
        # Master COGS List does, to column K - is not reported as raggedness.
        trimmed = list(row)
        while trimmed and not str(trimmed[-1]).strip():
            trimmed.pop()
        audit.field_counts[max(len(trimmed), 1) if trimmed else 0] += 1

    # header quality
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

    # raggedness
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

    # footer - from the tail when there is one, else the sample
    footer_source = tail if tail else rows
    if footer_source:
        last = footer_source[-1]
        populated = [(i, v) for i, v in enumerate(last) if str(v).strip()]
        if last and 0 < len(populated) < max(3, width * 0.4):
            leading_blank = all(not str(last[i]).strip() for i in range(min(3, len(last))))
            if leading_blank:
                audit.add(
                    "HANDLED" if len(populated) else "INFO",
                    f"trailing summary row: {len(populated)} of {width} cells populated "
                    f"at positions {[i for i, _ in populated]} - needs footer: totals_row",
                )
        if last and not any(str(v).strip() for v in last):
            audit.add("HANDLED", "trailing blank row - dropped by skip_trailing_blank_rows")

    # embedded newlines
    multiline = sum(1 for row in rows for cell in row if "\n" in str(cell))
    if multiline:
        audit.add("HANDLED", f"{multiline} cells contain embedded newlines")


def audit_csv(path: Path, *, full: bool) -> FileAudit:
    audit = FileAudit(name=path.name, kind="csv", size_mb=round(path.stat().st_size / 1e6, 2))

    head_raw = path.open("rb").read(2 * 1024 * 1024)
    text, encoding = _decode(head_raw)
    audit.encoding = encoding

    lines = text.splitlines()
    header_index = None
    for index, line in enumerate(lines[:30]):
        if line.lower().startswith("sep="):
            audit.preamble.append(line)
            continue
        cells = next(csv.reader([line]), [])
        if _looks_like_header(cells):
            header_index = index
            break
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

    # The tail is read separately so a footer is seen even when only a sample
    # of the head was taken. It is NOT merged into the raggedness sample:
    # seeking into the middle of the file lands mid-line, and the resulting
    # fragment counts as a short row that does not exist. That produced phantom
    # ragged-row findings on the Monthly Payments files.
    tail_rows: list[list[str]] = []
    if not full and path.stat().st_size > TAIL_BYTES:
        with path.open("rb") as handle:
            handle.seek(-TAIL_BYTES, io.SEEK_END)
            tail_text, _ = _decode(handle.read())
        # Drop the first two lines: the first is a fragment, and the second can
        # be too if the fragment contained an unbalanced quote.
        tail_rows = [r for r in csv.reader(tail_text.splitlines()[2:]) if r]

    _audit_rows(audit, header, rows, tail=tail_rows[-2:] if tail_rows else None)
    audit.tail_rows = rows[-2:]
    return audit


def audit_workbook(path: Path, *, full: bool) -> list[FileAudit]:
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

            grid = []
            for index, row in enumerate(worksheet.iter_rows(values_only=True)):
                if not full and index > SAMPLE_ROWS:
                    break
                grid.append([("" if c is None else str(c)) for c in row])

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
            header = grid[header_index]
            while header and not header[-1].strip():
                header.pop()
            _audit_rows(audit, header, grid[header_index + 1 :])
            out.append(audit)
    finally:
        workbook.close()
    return out


def audit_zip(path: Path, *, full: bool, limit: int = 3) -> list[FileAudit]:
    out: list[FileAudit] = []
    with zipfile.ZipFile(path) as archive:
        members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    audits: list[FileAudit] = []
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
        except Exception as exc:  # noqa: BLE001 - an audit must never abort the run
            broken = FileAudit(name=path.name, kind=path.suffix)
            broken.add("GAP", f"could not audit: {type(exc).__name__}: {exc}")
            audits.append(broken)

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
