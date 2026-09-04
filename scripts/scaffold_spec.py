"""Propose a source spec by profiling a real file.

This is the first half of source onboarding: point it at a vendor extract and
it emits a spec draft with types, PII classes and date formats inferred from
the data. The second half is a human reading the draft. Inference is a starting
point that saves an hour of transcription, never an authority - it cannot know
that Cardinal's ``Invoice Line #`` must stay text because its leading zeros are
significant, or that an ``Amount`` column carries accounting negatives.

    python scripts/scaffold_spec.py <file> --key wholesaler.abc.purchases \\
        [--sheet NAME] [--zip-member NAME] [--rows 2000]

Values are read to infer types and are never printed. The output contains
column names and inferred settings only, so a draft can be reviewed and
committed without carrying PHI.

Every draft is emitted with ``required: false`` on everything and a review
banner at the top. Guessing which fields are mandatory from a sample is exactly
the kind of inference that looks right and quarantines a year of history the
first time a legitimately empty value appears.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.registry.headers import is_junk_header, normalize_header  # noqa: E402

SAMPLE_ROWS = 2000

DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d-%b-%Y",
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%b %Y",
    "%B %Y",
    "%Y%m%d",
]

# Column-name signals. Matched against the normalized header, so "Patient DOB"
# and "PAT_DOB" both hit "dob".
PII_DIRECT = (
    "first_name", "last_name", "fname", "lname", "patient_name", "middle_name",
    "dob", "birth", "email", "phone", "cell", "street", "address", "zip_code",
)  # fmt: skip
PII_PROHIBITED = ("ssn", "social_security")
PII_QUASI = ("city", "state", "gender", "sex", "marital", "zip", "postal")
PII_JOIN = ("patient_id", "patient_identifier", "mrn", "pat_num", "patnum", "client_id", "uid")

MONEY_WORDS = (
    "amount", "cost", "price", "dollars", "fee", "payment", "paid", "revenue",
    "savings", "charge", "awp", "wac", "total", "subsidy", "receivable", "profit",
)  # fmt: skip
QTY_WORDS = ("qty", "quantity", "units", "count", "size", "rvu", "days_supply")


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _read_csv(handle: io.TextIOBase, limit: int) -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(handle)
    header: list[str] = []
    for row in reader:
        cells = [c for c in row if c.strip()]
        if len(cells) >= 3 and len(set(cells)) == len(cells):
            header = row
            break
    rows = []
    for index, row in enumerate(reader):
        if index >= limit:
            break
        rows.append(row)
    while header and not header[-1].strip():
        header.pop()
    return header, rows


def load_sample(
    path: Path, *, sheet: str | None, zip_member: str | None, limit: int
) -> tuple[list[str], list[list[str]]]:
    if zip_member:
        with zipfile.ZipFile(path) as archive, archive.open(zip_member) as raw:
            return _read_csv(io.StringIO(_decode(raw.read(20_000_000))), limit)

    if path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        import openpyxl

        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        worksheet = workbook[sheet] if sheet else workbook[workbook.sheetnames[0]]
        stream = worksheet.iter_rows(values_only=True)
        header = []
        for row in stream:
            cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if len(cells) >= 3 and len(set(cells)) == len(cells):
                header = [("" if c is None else str(c)) for c in row]
                break
        rows = []
        for index, row in enumerate(stream):
            if index >= limit:
                break
            rows.append([("" if c is None else str(c)) for c in row])
        workbook.close()
        while header and not header[-1].strip():
            header.pop()
        return header, rows

    return _read_csv(path.open("r", encoding="utf-8-sig", errors="replace", newline=""), limit)


def _values_for(rows: list[list[str]], index: int) -> list[str]:
    out = []
    for row in rows:
        if index < len(row):
            value = str(row[index]).strip()
            if value and value.lower() not in {"nan", "none", "null", "n/a", "-"}:
                out.append(value)
    return out


def _detect_date_formats(values: list[str]) -> list[str]:
    from datetime import datetime

    hits = []
    for fmt in DATE_FORMATS:
        matched = 0
        for value in values[:200]:
            try:
                datetime.strptime(value, fmt)
                matched += 1
            except ValueError:
                pass
        if matched and matched >= len(values[:200]) * 0.95:
            hits.append(fmt)
    return hits


def _is_money(values: list[str]) -> bool:
    signals = sum(1 for v in values[:200] if v.startswith(("$", "(")) or v.endswith(")"))
    return signals >= max(1, len(values[:200]) * 0.05)


def _numeric_kind(values: list[str]) -> str | None:
    sample = values[:300]
    if not sample:
        return None
    whole = 0
    for value in sample:
        cleaned = value.replace(",", "").replace("$", "").strip()
        cleaned = cleaned[1:-1] if cleaned.startswith("(") and cleaned.endswith(")") else cleaned
        try:
            number = Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return None
        if number == number.to_integral_value():
            whole += 1
    return "integer" if whole == len(sample) else "decimal"


def _pii_class(normalized: str) -> str:
    if any(w in normalized for w in PII_PROHIBITED):
        return "prohibited"
    if any(w in normalized for w in PII_DIRECT):
        return "direct"
    if any(w in normalized for w in PII_JOIN):
        return "join_key"
    if any(w in normalized for w in PII_QUASI):
        return "quasi"
    return "none"


def infer_column(header: str, values: list[str]) -> dict:
    normalized = normalize_header(header)
    column: dict = {"target": normalized or "column", "sources": [header], "type": "text"}

    if "ndc" in normalized:
        column["type"] = "ndc"
    else:
        formats = _detect_date_formats(values)
        if formats and ("date" in normalized or "month" in normalized or len(formats) == 1):
            column["type"] = "date"
            column["date_formats"] = formats
        else:
            kind = _numeric_kind(values)
            if kind:
                money = _is_money(values) or any(w in normalized for w in MONEY_WORDS)
                quantity = any(w in normalized for w in QTY_WORDS)
                column["type"] = "currency" if money and not quantity else kind
            elif _is_money(values):
                column["type"] = "currency"
            elif {v.lower() for v in values[:100]} <= {"true", "false", "y", "n", "yes", "no"}:
                if values:
                    column["type"] = "boolean"

    pii = _pii_class(normalized)
    if pii != "none":
        column["pii"] = pii
    if column["type"] == "currency":
        column["control_total"] = True
    return column


def render(key: str, path: Path, header: list[str], rows: list[list[str]]) -> str:
    lines = [
        "# DRAFT - generated by scripts/scaffold_spec.py. Review before use.",
        "#",
        f"# Profiled from: {path.name}",
        f"# Columns: {len(header)}   sample rows: {len(rows)}",
        "#",
        "# Inference is a starting point, not an authority. Before committing:",
        "#   - mark the natural key, and required columns (all default to false;",
        "#     guessing which fields are mandatory from a sample quarantines a",
        "#     year of history the first time a legitimate blank appears)",
        "#   - check every date_formats list against a second delivery",
        "#   - keep identifiers with leading zeros as text, never integer",
        "#   - confirm control_total only on additive amounts, never on rates",
        "#   - confirm the PII classes, especially anything marked prohibited",
        "#",
        f"key: {key}",
        f"name: TODO {key}",
        "vendor: TODO",
        "group: TODO",
        "lifecycle: active",
        "",
        "filename_patterns:",
        "  - TODO",
        "",
        "natural_key: []  # TODO",
        "",
        "on_new_column: fail",
        "on_missing_column: fail",
        "",
        "variants:",
        "  - name: v1",
        "    detect:",
        "      require_all: []  # TODO: columns that identify this shape",
        "    columns:",
    ]
    for index, raw_header in enumerate(header):
        if is_junk_header(raw_header):
            lines.append(f"      # skipped export artifact: {raw_header!r}")
            continue
        column = infer_column(raw_header, _values_for(rows, index))
        lines.append(f"      - target: {column['target']}")
        lines.append(f"        sources: [{column['sources'][0]!r}]")
        lines.append(f"        type: {column['type']}")
        if "date_formats" in column:
            formats = ", ".join(repr(f) for f in column["date_formats"])
            lines.append(f"        date_formats: [{formats}]")
        if "pii" in column:
            lines.append(f"        pii: {column['pii']}")
        if column.get("control_total"):
            lines.append("        control_total: true")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--key", required=True)
    parser.add_argument("--sheet")
    parser.add_argument("--zip-member")
    parser.add_argument("--rows", type=int, default=SAMPLE_ROWS)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    header, rows = load_sample(
        args.file, sheet=args.sheet, zip_member=args.zip_member, limit=args.rows
    )
    if not header:
        print(f"could not find a header row in {args.file.name}", file=sys.stderr)
        return 1

    duplicates = [h for h, n in Counter(normalize_header(h) for h in header).items() if n > 1]
    if duplicates:
        print(f"warning: duplicate normalized headers {duplicates}", file=sys.stderr)

    text = render(args.key, args.file, header, rows)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(header)} columns)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
