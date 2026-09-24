"""One-time bootstrap: emit final source specs from real headers + curation.

The curation table below is the human judgement - natural keys, required
columns, footers, PII, type overrides. Column lists come from the actual files
so nothing is transcribed by hand and no header typo is introduced.

Output is committed YAML, read and edited by people from then on.
"""

from __future__ import annotations

import csv
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
#: Where the vendor extracts live - PHI, kept outside this repository.
#: Per-machine, so set SBH_SOURCE_ROOT rather than editing this.
ROOT = Path(
    os.environ.get(
        "SBH_SOURCE_ROOT",
        r"C:\Users\SharanLokeshwar\OneDrive - Sagebrush Healthcare\Sagebrush ETL Files",
    )
)
DROP = ROOT / "Original Files" / "Sagebrush ETL files 03062026"
NEW_DROP = (
    ROOT / "Original Files" / "March012026-June102026" / "Sagebrush ETL files_ Mar012026-June102026"
)

sys.path.insert(0, str(REPO / "src"))
from sbh_data_bank.registry.headers import is_junk_header, normalize_header  # noqa: E402


def headers_of(path: Path, sheet: str | None = None, preamble: int = 0) -> list[str]:
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb[wb.sheetnames[0]]
        stream = ws.iter_rows(values_only=True)
        for _ in range(preamble):
            next(stream, None)
        row = next(stream, ())
        wb.close()
        cells = [("" if c is None else str(c)) for c in row]
    else:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            for _ in range(preamble):
                fh.readline()
            cells = next(csv.reader(fh), [])
    while cells and not cells[-1].strip():
        cells.pop()
    return cells


def sample_values(path: Path, preamble: int = 0, limit: int = 1500) -> dict[str, list[str]]:
    """Column values from the head of a file, for value-based type detection.

    Typing by column name is unreliable and was caught being so: a name rule
    over Curve Monthly Services matched PostDate, SvcDate and BillDate but
    missed DOB, SvcDateFrom, SvcDateTo, BillDateOrig, BillDateInit and two
    opaquely named columns (PIF_4, PIF_5) that also hold dates. Seven real date
    columns would have landed as text.
    """
    csv.field_size_limit(10_000_000)
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        for _ in range(preamble):
            fh.readline()
        reader = csv.DictReader(fh)
        out: dict[str, list[str]] = {h: [] for h in (reader.fieldnames or [])}
        for index, row in enumerate(reader):
            if index >= limit:
                break
            for header in out:
                value = (row.get(header) or "").strip()
                if value:
                    out[header].append(value)
    return out


DATE_CANDIDATES = [
    ("%m/%d/%Y %H:%M", ["%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"]),
    ("%m/%d/%Y", ["%m/%d/%Y", "%Y-%m-%d"]),
    ("%Y-%m-%d %H:%M:%S", ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]),
    ("%Y-%m-%d", ["%Y-%m-%d", "%m/%d/%Y"]),
]


def detect_date_formats(values: list[str], threshold: float = 0.98) -> list[str] | None:
    """The format list for a column, or None if it is not a date column.

    Requires near-total agreement, so a text column with a few date-like values
    is not mistyped into quarantining every other row.
    """
    from datetime import datetime

    sample = values[:400]
    if len(sample) < 5:
        return None
    for probe, formats in DATE_CANDIDATES:
        matched = 0
        for value in sample:
            try:
                datetime.strptime(value, probe)
                matched += 1
            except ValueError:
                pass
        if matched >= len(sample) * threshold:
            return formats
    return None


def target_of(header: str) -> str:
    name = normalize_header(header)
    name = re.sub(r"^(\d)", r"n\1", name)
    return name or "column"


# --- curation -------------------------------------------------------------
# type overrides, PII, required, control totals, and per-source prose.

MONEY = "currency"
NUM = "decimal"
INT = "integer"
DATE = "date"
NDC = "ndc"
TEXT = "text"
BOOL = "boolean"

US_DATE = ["%m/%d/%Y", "%Y-%m-%d"]
ISO_DATE = ["%Y-%m-%d", "%m/%d/%Y"]


def spec_yaml(
    *,
    key: str,
    name: str,
    vendor: str,
    group: str,
    catalog_names: list[str],
    header: str,
    filename_patterns: list[str],
    natural_key: list[str],
    variants: list[dict],
    lifecycle: str = "active",
    cadence: int | None = 7,
    coverage_start: str | None = None,
    on_new_column: str = "warn",
    on_missing_column: str = "fail",
    constants: dict[str, str] | None = None,
    notes: str = "",
) -> str:
    out: list[str] = [header.rstrip(), ""]
    out.append(f"key: {key}")
    out.append(f"name: {name}")
    out.append(f"vendor: {vendor}")
    out.append(f'group: "{group}"')
    out.append(f"catalog_names: [{', '.join(repr(c) for c in catalog_names)}]")
    out.append(f"lifecycle: {lifecycle}")
    out.append(f"expected_cadence_days: {cadence if cadence is not None else 'null'}")
    if coverage_start:
        out.append(f'coverage_start: "{coverage_start}"')
    out.append("")
    out.append("filename_patterns:")
    for pattern in filename_patterns:
        out.append(f"  - {pattern!r}")
    out.append("")
    out.append(f"natural_key: [{', '.join(natural_key)}]")
    if constants:
        out.append("constants:")
        for k, v in constants.items():
            out.append(f"  {k}: {v!r}")
    out.append("")
    out.append(f"on_new_column: {on_new_column}")
    out.append(f"on_missing_column: {on_missing_column}")
    if notes:
        out.append(f"notes: >")
        for line in notes.strip().splitlines():
            out.append(f"  {line.strip()}")
    out.append("")
    out.append("variants:")

    for variant in variants:
        out.append(f"  - name: {variant['name']}")
        if variant.get("action"):
            out.append(f"    action: {variant['action']}")
        if variant.get("superseded_by"):
            out.append(f"    superseded_by: {variant['superseded_by']}")
        if variant.get("description"):
            out.append("    description: >")
            for line in variant["description"].strip().splitlines():
                out.append(f"      {line.strip()}")
        out.append("    detect:")
        out.append(f"      require_all: [{', '.join(repr(c) for c in variant['require_all'])}]")
        if variant.get("require_none"):
            out.append(
                f"      require_none: [{', '.join(repr(c) for c in variant['require_none'])}]"
            )
        read = variant.get("read") or {}
        if read:
            out.append("    read:")
            for k, v in read.items():
                out.append(f"      {k}: {v!r}" if isinstance(v, str) else f"      {k}: {v}")
        out.append("    columns:")

        overrides = variant.get("overrides", {})
        aliases = variant.get("aliases", {})
        # rename maps a vendor header to a target that differs from its
        # normalized form. Used where a vendor renames a column between
        # exports and both must land under one canonical name - WeInfuse's
        # "Inner NDC" and "Inner NDC Code" are the same field.
        rename = variant.get("rename", {})
        for raw in variant["headers"]:
            if is_junk_header(raw):
                out.append("      # unnamed export artifact at this position, not data")
                continue
            target = rename.get(raw, target_of(raw))
            spec = overrides.get(target, {})
            sources = [raw] + [a for a in aliases.get(target, []) if a != raw]
            out.append(f"      - target: {target}")
            out.append(f"        sources: [{', '.join(repr(s) for s in sources)}]")
            out.append(f"        type: {spec.get('type', TEXT)}")
            if spec.get("type") == DATE:
                fmts = spec.get("formats", US_DATE)
                out.append(f"        date_formats: [{', '.join(repr(f) for f in fmts)}]")
            if spec.get("required"):
                out.append("        required: true")
            if spec.get("pii"):
                out.append(f"        pii: {spec['pii']}")
            if spec.get("control_total"):
                out.append("        control_total: true")
            if spec.get("desc"):
                out.append("        description: >")
                for line in spec["desc"].strip().splitlines():
                    out.append(f"          {line.strip()}")
    return "\n".join(out) + "\n"


def write(filename: str, text: str) -> None:
    (REPO / "specs" / filename).write_text(text, encoding="utf-8")
    print(f"wrote specs/{filename} ({len(text.splitlines())} lines)")
