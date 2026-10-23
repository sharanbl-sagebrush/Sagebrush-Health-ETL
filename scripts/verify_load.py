"""Hold the database against the raw files: is everything loaded, is every
row there, and what did deduplication do.

    python scripts/verify_load.py --env-file .env
    python scripts/verify_load.py --env-file .env --root <dir> --only <spec key>

Three parts, all read-only.

1. **Every file under the root.** Each data file (zip members included)
   is hashed and looked up in ``meta.source_file``. It must be LOADED,
   a byte-identical copy of a loaded file (``DUPLICATE_FILE`` or the same
   hash under another path), recognised and deliberately not loaded, or
   on the unclaimed list. Anything else is a gap and is listed.
2. **Every loaded file's rows.** The file's data lines are counted again
   with the plain ``csv`` module or ``openpyxl``, using only the spec's
   header location (preamble or marker) and trailing blank rows dropped,
   and compared with ``rows_landed`` on the batch. A mismatch means the
   load read a different number of lines than the file holds.
3. **Every source in the database.** Current rows, superseded rows, rows
   landed in all, the identities the vendor sent more than once in one
   file (occurrences), the duplicate findings, quarantine, conflicts,
   refusals, and the deliveries in as-of order.

The verdict is the last line; the exit code is 1 when anything is wrong.

PHI: prints filenames, counts, hashes' first eight characters, outcomes
and spec keys. Never a cell value.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import os
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.commit.discover import DATA_SUFFIXES, DEFAULT_SKIP_DIRS, unclaimed  # noqa: E402
from sbh_data_bank.registry.headers import normalize_header  # noqa: E402
from sbh_data_bank.registry.loader import SpecRegistry  # noqa: E402
from sbh_data_bank.registry.spec import ReadSpec, SourceSpec  # noqa: E402
from sbh_data_bank.store.config import SqlSettings, load_env_file  # noqa: E402
from sbh_data_bank.store.connection import Database  # noqa: E402
from sbh_data_bank.store.schema import table_name  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path(
    os.environ.get(
        "SBH_SOURCE_ROOT",
        r"C:\Users\SharanLokeshwar\OneDrive - Sagebrush Healthcare\Sagebrush ETL Files",
    )
)
BENIGN = {"LOADED", "DUPLICATE_FILE", "RECOGNISED_NOT_LOADED"}


def _sha256(data: bytes | Path) -> str:
    digest = hashlib.sha256()
    if isinstance(data, Path):
        with open(data, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    else:
        digest.update(data)
    return digest.hexdigest()


def _is_blank(row: list[Any]) -> bool:
    return all(v is None or str(v).strip() == "" for v in row)


def _count_after_header(rows: list[list[Any]], read: ReadSpec) -> int | None:
    """Data lines after the header the spec's read settings locate, with
    trailing blank rows dropped. ``None`` when the header is not found."""
    if read.header_marker is not None:
        wanted = normalize_header(read.header_marker)
        header_at = None
        for i, row in enumerate(rows[: read.max_preamble_scan]):
            if any(normalize_header(c) == wanted for c in row):
                header_at = i
                break
        if header_at is None:
            return None
    else:
        header_at = read.preamble_lines
    data = rows[header_at + 1 :]
    while data and _is_blank(data[-1]):
        data.pop()
    return len(data)


def _csv_rows(data: bytes, read: ReadSpec) -> list[list[Any]]:
    text = io.TextIOWrapper(io.BytesIO(data), encoding=read.encoding, errors="strict", newline="")
    return list(csv.reader(text, delimiter=read.delimiter))


def _sheet_rows(path: Path, sheet: str) -> list[list[Any]]:
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return [list(r) for r in workbook[sheet].iter_rows(values_only=True)]
    finally:
        workbook.close()


def _read_for(spec: SourceSpec, variant_name: str | None) -> ReadSpec:
    for v in spec.variants:
        if v.name == variant_name:
            return v.read
    return spec.variants[0].read


def _files(root: Path) -> list[tuple[Path, str | None]]:
    """Every data file under the root, as (path, zip member or None)."""
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(p in DEFAULT_SKIP_DIRS for p in path.parts):
            continue
        suffix = path.suffix.lower()
        if suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                found.extend(
                    (path, m) for m in sorted(archive.namelist()) if m.lower().endswith(".csv")
                )
        elif suffix in DATA_SUFFIXES:
            found.append((path, None))
    return found


def part_files(db: Database, registry: SpecRegistry, root: Path, tenant: str, only: set[str]):
    print("\n=== 1. every file under the root")
    by_hash: dict[str, list[tuple]] = defaultdict(list)
    for row in db.execute(
        "SELECT sf.file_sha256, sf.source_key, sf.sheet_name, sf.file_name, b.outcome, "
        "b.rows_landed, b.variant_name, b.started_at FROM meta.source_file sf "
        "LEFT JOIN meta.batch b ON b.source_file_id = sf.source_file_id "
        "WHERE sf.tenant_id = ? ORDER BY b.started_at",
        tenant,
    ):
        by_hash[row[0]].append(row)
    orphans = {p.as_posix() for p in unclaimed(registry, root)}
    counts: Counter = Counter()
    mismatches = []
    gaps = []
    seen_orphans: set[str] = set()
    for path, member in _files(root):
        rel = path.relative_to(root).as_posix() + (f"::{member}" if member else "")
        if member:
            with zipfile.ZipFile(path) as archive:
                data = archive.read(member)
            digest = _sha256(data)
        else:
            data = None
            digest = _sha256(path)
        records = by_hash.get(digest, [])
        if not records:
            if path.relative_to(root).as_posix() in orphans or rel in orphans:
                if path.as_posix() not in seen_orphans:
                    seen_orphans.add(path.as_posix())
                    counts["unclaimed"] += 1
                continue
            counts["NOT IN DATABASE"] += 1
            gaps.append(rel)
            print(f"  NOT IN DATABASE  {rel}")
            continue
        # One file may be several worksheets and several batches; judge each sheet.
        sheets = defaultdict(list)
        for r in records:
            sheets[(r[1], r[2])].append(r)
        for (source_key, sheet), rows in sheets.items():
            if only and source_key not in only:
                continue
            outcomes = [r[4] for r in rows]
            loaded = [r for r in rows if r[4] == "LOADED"]
            if loaded:
                counts["loaded"] += 1
                spec = registry.get(source_key)
                read = _read_for(spec, loaded[0][6])
                if member:
                    raw = _csv_rows(data, read)
                elif path.suffix.lower() in {".xlsx", ".xlsm"}:
                    raw = _sheet_rows(path, sheet)
                else:
                    raw = _csv_rows(path.read_bytes(), read)
                lines = _count_after_header(raw, read)
                landed = loaded[0][5]
                if lines != landed:
                    counts["ROW COUNT MISMATCH"] += 1
                    mismatches.append((rel, sheet, lines, landed))
                    print(f"  ROW COUNT MISMATCH  {rel} [{sheet}] file={lines} landed={landed}")
            elif outcomes[-1] in BENIGN:
                # The latest attempt is what counts: a file refused before the
                # spec learned its shape and recognised since is settled.
                counts["duplicate or recognised"] += 1
            else:
                counts["REFUSED, never loaded"] += 1
                gaps.append(rel)
                print(f"  REFUSED  {rel} [{sheet}] {sorted(set(outcomes))}")
    print("  " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return gaps, mismatches


def part_sources(db: Database, registry: SpecRegistry, tenant: str, only: set[str]):
    print("\n=== 3. every source in the database")
    problems = []
    head = f"{'source':<36}{'current':>9}{'superseded':>11}{'landed':>9}{'dup ids':>8}"
    head += f"{'findings':>9}{'quar':>5}{'conf':>5}{'refused':>8}{'files':>6}"
    print(head)
    for spec in registry:
        if only and spec.key not in only:
            continue
        cur = table_name(spec, "curated")
        q = db.scalar
        current = q(
            f"SELECT COUNT(*) FROM {cur} WHERE tenant_id = ? AND superseded_by IS NULL", tenant
        )
        superseded = q(
            f"SELECT COUNT(*) FROM {cur} WHERE tenant_id = ? AND superseded_by IS NOT NULL", tenant
        )
        landed = q(f"SELECT COUNT(*) FROM {table_name(spec, 'landed')} WHERE tenant_id = ?", tenant)
        double = q(
            f"SELECT COUNT(*) FROM (SELECT identity_hash, occurrence FROM {cur} "
            "WHERE tenant_id = ? AND superseded_by IS NULL GROUP BY identity_hash, occurrence "
            "HAVING COUNT(*) > 1) t",
            tenant,
        )
        dup_ids = q(
            f"SELECT COUNT(DISTINCT identity_hash) FROM {cur} "
            "WHERE tenant_id = ? AND superseded_by IS NULL AND occurrence > 1",
            tenant,
        )
        findings = db.execute(
            "SELECT identical, COUNT(*) FROM stg.duplicate_finding WHERE tenant_id = ? "
            "AND source_key = ? GROUP BY identical",
            tenant,
            spec.key,
        )
        quarantine = q(
            "SELECT COUNT(*) FROM stg.quarantine WHERE tenant_id = ? AND source_key = ?",
            tenant,
            spec.key,
        )
        batches = {
            r[0]: (int(r[1]), int(r[2] or 0))
            for r in db.execute(
                "SELECT outcome, COUNT(*), SUM(rows_conflict) FROM meta.batch "
                "WHERE tenant_id = ? AND source_key = ? GROUP BY outcome",
                tenant,
                spec.key,
            )
        }
        conflicts = sum(v[1] for v in batches.values())
        refused = sum(n for o, (n, _) in batches.items() if o not in BENIGN)
        files = batches.get("LOADED", (0, 0))[0]
        order = db.execute(
            "SELECT sf.as_of, sf.delivery_rank FROM meta.source_file sf JOIN meta.batch b "
            "ON b.source_file_id = sf.source_file_id AND b.outcome = 'LOADED' "
            "WHERE sf.tenant_id = ? AND sf.source_key = ? ORDER BY b.started_at",
            tenant,
            spec.key,
        )
        as_ofs = [r[0] for r in order]
        in_order = as_ofs == sorted(as_ofs)
        f_identical = sum(n for ident, n in findings if ident)
        f_differ = sum(n for ident, n in findings if not ident)
        line = f"{spec.key:<36}{current:>9}{superseded:>11}{landed:>9}{dup_ids:>8}"
        line += f"{f_identical + f_differ:>9}{quarantine:>5}{conflicts:>5}{refused:>8}{files:>6}"
        flags = []
        if double:
            flags.append(f"{double} identities with two current rows")
        if not in_order:
            flags.append("deliveries not committed in as-of order")
        if f_differ:
            flags.append(f"{f_differ} in-file duplicate keys with differing content")
        if quarantine or conflicts or refused:
            flags.append("quarantine/conflict/refusal on record")
        print(line + ("   <- " + "; ".join(flags) if flags else ""))
        if double or not in_order:
            problems.append((spec.key, flags))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--specs", type=Path, default=REPO / "specs")
    args = parser.parse_args()
    registry = SpecRegistry.from_dir(args.specs)
    if args.env_file:
        load_env_file(args.env_file)
    settings = SqlSettings.from_env()
    only = set(args.only)
    print(f"verifying {settings.describe()} against {args.root}")
    with Database(settings) as db:
        gaps, mismatches = part_files(db, registry, args.root.resolve(), settings.tenant, only)
        problems = part_sources(db, registry, settings.tenant, only)
    bad = len(gaps) + len(mismatches) + len(problems)
    print(
        f"\nverdict: {len(gaps)} files not loaded, {len(mismatches)} row-count mismatches, "
        f"{len(problems)} sources with a structural problem"
    )
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
