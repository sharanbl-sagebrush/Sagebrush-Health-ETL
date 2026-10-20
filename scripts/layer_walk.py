"""Walk one source's real files through the four storage layers, in memory.

    python scripts/layer_walk.py wholesaler.mckesson.invoice
    python scripts/layer_walk.py wholesaler.mckesson.invoice --out docs/layer-walk
    python scripts/layer_walk.py --all --out docs/layer-walk

Why this exists
---------------
The commit engine is not built, so nothing has been written to the layered
tables yet. This script does what the engine will do, file by file, without a
database, and reports what each layer would hold. It is the reviewer's view:
"if we load this source today, exactly what happens to every file and every
row". It reuses the real reader, binder and typer (``ingest.load``), the real
delivery facts (``curate.delivery``: as-of, coverage, the as-of floor) and
the real dedup rule (``curate.rule``), so it cannot drift from the pipeline.

What it reports, per source
---------------------------
Delivery  (layer 0, ``meta.source_file``): every file the spec's filename
          patterns find under the root, its content hash, size, as-of date
          and where that date came from (decision D5), the window the
          filename claims, and whether it is a byte-identical duplicate of a
          file already seen (refused, never landed twice).
Landed    (layer 1): lines after the header, the footer if recognised, the
          columns the vendor sent that the spec does not claim (kept as
          ``extra_cells``), optional columns the file lacks, and prohibited
          columns matched and discarded.
Typed     (layer 2): rows typed, rows quarantined by (column, reason), the
          row-accounting balance, control totals against the footer, and the
          as-of floor check (decision D4).
Curated   (layer 3): the dedup rule applied delivery by delivery in as-of
          order - inserted / unchanged / corrected / late / conflict /
          vanished per delivery, with the coverage window each delivery spoke
          for - plus within-file duplicate identities (occurrence > 1) and
          corrections that are only a reordering of colliding lines. Then
          the late-file test: the oldest delivery is replayed last and the
          current state must not move.
Presented (layer 4): what is current at the end - row count and control
          totals over current rows only.

PHI: the vendor files are PHI. This script prints counts, column names,
reason codes, filenames and vendor footer totals. It never prints a cell
value, a key value or a quarantine payload. Keep it that way.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.core.identity import file_sha256  # noqa: E402
from sbh_data_bank.curate.delivery import (  # noqa: E402
    AsOfUnavailableError,
    Coverage,
    as_of_floor_violation,
    filename_window,
    resolve_as_of,
    resolve_coverage,
)
from sbh_data_bank.curate.rule import DeliveryFacts, Ledger, RowFact, facts_for  # noqa: E402
from sbh_data_bank.ingest.load import FileLoad, load_file, load_workbook  # noqa: E402
from sbh_data_bank.registry.loader import SpecRegistry  # noqa: E402
from sbh_data_bank.registry.spec import PiiClass, SourceSpec  # noqa: E402
from sbh_data_bank.store.schema import DEDUP_RULE, identity_columns_for  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path(r"C:\Users\SharanLokeshwar\OneDrive - Sagebrush Healthcare\Sagebrush ETL Files")
DATA_SUFFIXES = {".csv", ".xlsx", ".xls"}
#: Folders under the root that are not vendor deliveries.
#: "Sagebrush ETL Test Files" holds hand-cut subsets of real files made for
#: testing, not vendor deliveries, so they are listed by name but not walked.
SKIP_DIRS = {"Azure App ETL Files - Consultant", "Sagebrush ETL Test Files"}

# --------------------------------------------------------------------------
# Layer 0: delivery facts - as-of date and content identity
# --------------------------------------------------------------------------


@dataclass
class Delivery:
    """One file, or one worksheet, or one zip member: the unit the engine loads."""

    label: str
    path: Path
    member: str | None = None
    sheet: str | None = None
    sha256: str = ""
    size: int = 0
    as_of: date | None = None
    as_of_source: str = "none"
    as_of_problem: str = ""
    window: tuple[date | None, date | None] = (None, None)
    duplicate_of: str | None = None

    @property
    def display_name(self) -> str:
        name = self.member or self.path.name
        return f"{name} [{self.sheet}]" if self.sheet else name


def discover(spec: SourceSpec, root: Path) -> list[Delivery]:
    """Every delivery of this source under the root, by filename pattern.

    The filename is a routing hint; the binder decides what a file is when it
    is loaded. A file the pattern finds but the spec cannot bind is reported,
    not hidden. Zip archives whose own name matches the spec are opened and
    each CSV member is a delivery (Monthly Payments ships 40 months that way).
    The as-of comes from ``curate.delivery.resolve_as_of``, the same chain
    the engine will use; a file it refuses is listed with the reason.
    """
    found: list[Delivery] = []
    seen_hash: dict[str, str] = {}

    def register(d: Delivery, filename: str) -> None:
        d.window = filename_window(filename)
        try:
            as_of = resolve_as_of(d.path, spec, filename=filename, root=root)
            d.as_of, d.as_of_source = as_of.value, as_of.source.value
        except AsOfUnavailableError as exc:
            d.as_of_problem = str(exc)
        if d.sha256 in seen_hash:
            d.duplicate_of = seen_hash[d.sha256]
        else:
            seen_hash[d.sha256] = d.display_name
        found.append(d)

    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        suffix = path.suffix.lower()
        if suffix == ".zip" and spec.matches_filename(path.stem):
            with zipfile.ZipFile(path) as archive:
                for member in sorted(archive.namelist()):
                    if not member.lower().endswith(".csv"):
                        continue
                    data = archive.read(member)
                    d = Delivery(label=member, path=path, member=member)
                    d.sha256 = hashlib.sha256(data).hexdigest()
                    d.size = len(data)
                    register(d, Path(member).name)
            continue
        if suffix not in DATA_SUFFIXES or not spec.matches_filename(path.name):
            continue
        d = Delivery(label=path.name, path=path)
        d.sha256 = file_sha256(path)
        d.size = path.stat().st_size
        register(d, path.name)
    return found


# --------------------------------------------------------------------------
# Layers 1-2: land and type one delivery with the real loader
# --------------------------------------------------------------------------


@dataclass
class DeliveryWalk:
    """Everything the walk learned about one delivery, layers 0 to 2."""

    delivery: Delivery
    outcome: str = ""
    detail: str = ""
    variant: str | None = None
    rows_read: int = 0
    rows_ok: int = 0
    rows_quarantined: int = 0
    balances: bool = True
    footer_line: int | None = None
    footer_agrees: bool | None = None
    footer_mismatches: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    control_totals: dict[str, Decimal] = field(default_factory=dict)
    new_columns: tuple[str, ...] = ()
    missing_optional: tuple[str, ...] = ()
    prohibited_matched: tuple[str, ...] = ()
    quarantine_reasons: Counter = field(default_factory=Counter)
    rows: list[RowFact] = field(default_factory=list)
    coverage: Coverage | None = None
    floor_problem: str | None = None
    dup_groups: int = 0
    dup_rows: int = 0
    dup_identical_groups: int = 0
    max_occurrence: int = 1


def _loads_for(spec: SourceSpec, d: Delivery) -> list[FileLoad]:
    if d.member is not None:
        with zipfile.ZipFile(d.path) as archive:
            data = archive.read(d.member)
        return [load_file(io.BytesIO(data), spec, filename=Path(d.member).name, keep_landed=False)]
    if d.path.suffix.lower() == ".csv":
        return [load_file(d.path, spec, keep_landed=False)]
    return load_workbook(d.path, spec, keep_landed=False)


def walk_delivery(spec: SourceSpec, d: Delivery, load: FileLoad) -> DeliveryWalk:
    """Land and type one delivery; reduce every typed row to a RowFact."""
    w = DeliveryWalk(delivery=d, outcome=load.outcome, detail=load.detail)
    w.variant = load.variant_name
    c = load.counters
    w.rows_read, w.rows_ok, w.rows_quarantined = c.rows_read, c.rows_ok, c.rows_quarantined
    w.balances = c.balances
    w.control_totals = dict(c.control_totals)
    if load.footer is not None:
        w.footer_line = load.footer.line_number
        w.footer_agrees = load.footer.agrees
        w.footer_mismatches = load.footer.mismatches
    if load.binding is not None:
        w.new_columns = load.binding.new_columns
        w.missing_optional = load.binding.missing_optional
        w.prohibited_matched = tuple(
            b.column.target for b in load.binding.present if b.column.pii is PiiClass.PROHIBITED
        )
    for q in load.quarantined:
        for e in q.errors:
            w.quarantine_reasons[(e.column, e.reason)] += 1

    if d.as_of is None:
        return w
    controls = [t for t, flagged in _control_targets(spec) if flagged]
    w.rows = facts_for(spec, load.rows, as_of=d.as_of, controls=controls)
    # The as-of floor (D4) and coverage (D2), both over the declared columns.
    event_column = spec.event_date_column
    if event_column is not None:
        w.floor_problem = as_of_floor_violation(
            d.as_of, (r.values.get(event_column) for r in load.rows)
        )
    coverage_column = spec.coverage_target
    w.coverage = resolve_coverage(
        spec,
        filename=Path(d.member).name if d.member else d.path.name,
        as_of=d.as_of,
        coverage_dates=(r.values.get(coverage_column) for r in load.rows)
        if coverage_column
        else (),
    )
    counts: Counter = Counter(f.identity_hash for f in w.rows)
    hashes_by_identity: dict[str, set[str]] = {}
    for f in w.rows:
        hashes_by_identity.setdefault(f.identity_hash, set()).add(f.row_hash)
    groups = {ih: n for ih, n in counts.items() if n > 1}
    w.dup_groups = len(groups)
    w.dup_rows = sum(groups.values())
    w.dup_identical_groups = sum(1 for ih in groups if len(hashes_by_identity[ih]) == 1)
    w.max_occurrence = max(counts.values(), default=1)
    return w


def _control_targets(spec: SourceSpec) -> list[tuple[str, bool]]:
    seen: dict[str, bool] = {}
    for v in spec.variants:
        for col in v.columns:
            seen[col.target] = seen.get(col.target, False) or col.control_total
    return list(seen.items())


def _facts(w: DeliveryWalk) -> DeliveryFacts:
    assert w.delivery.as_of is not None
    return DeliveryFacts(w.delivery.display_name, w.delivery.as_of, w.coverage, w.rows)


# --------------------------------------------------------------------------
# The walk and its report
# --------------------------------------------------------------------------


def _semantics_line(spec: SourceSpec) -> str:
    """One sentence on the spec's delivery semantics, for the report header."""
    if spec.event_date is None:
        return "No event date declared: no as-of floor, no coverage, no vanish."
    parts = [f"event date `{spec.event_date}`"]
    window = f"coverage from {spec.coverage.value}"
    if spec.coverage_column:
        window += f" compared on `{spec.coverage_column}`"
    if spec.coverage_start_offset_days or spec.coverage_end_offset_days:
        window += (
            f" (offsets {spec.coverage_start_offset_days:+d}/"
            f"{spec.coverage_end_offset_days:+d} days)"
        )
    parts.append(window)
    if spec.vanishes:
        scope = f"scoped by {', '.join(spec.vanish_scope)}" if spec.vanish_scope else "unscoped"
        parts.append(f"vanish on, {scope}")
    else:
        parts.append("vanish off")
    if spec.row_hash_exclude:
        parts.append(f"row hash excludes {', '.join(spec.row_hash_exclude)}")
    return "; ".join(parts) + "."


def walk_source(spec: SourceSpec, root: Path) -> str:
    deliveries = discover(spec, root)
    controls = [t for t, flagged in _control_targets(spec) if flagged]
    identity = identity_columns_for(spec)

    out: list[str] = []
    p = out.append
    p(f"# Layer walk: {spec.key}")
    p("")
    p(f"{spec.name}. Generated by `scripts/layer_walk.py` on {date.today().isoformat()}.")
    p(
        f"Dedup rule `{DEDUP_RULE}`; identity = "
        + ("natural key " if spec.natural_key else "all columns ")
        + f"({len(identity)}: {', '.join(identity) if spec.natural_key else 'every target'})"
        + " + occurrence. "
        + _semantics_line(spec)
    )
    p("")

    # ---- Layer 0 -------------------------------------------------------
    p("## Layer 0 - deliveries found")
    p("")
    if not deliveries:
        p("No file under the root matches this spec's filename patterns.")
        return "\n".join(out)
    p("| # | file | bytes | as-of | from | filename window | note |")
    p("|---|---|---:|---|---|---|---|")
    for i, d in enumerate(deliveries, 1):
        window = " to ".join(x.isoformat() if x else "?" for x in d.window)
        note = (
            f"duplicate of #{[x.display_name for x in deliveries].index(d.duplicate_of) + 1}"
            if d.duplicate_of
            else ""
        )
        if d.as_of_problem:
            note = f"REFUSED: {d.as_of_problem}"
        p(
            f"| {i} | {d.display_name} | {d.size:,} | {d.as_of or '-'} | "
            f"{d.as_of_source} | {window} | {note} |"
        )
    p("")

    # ---- Layers 1-2 ----------------------------------------------------
    walks: list[DeliveryWalk] = []
    for d in deliveries:
        if d.duplicate_of:
            continue
        try:
            loads = _loads_for(spec, d)
        except Exception as exc:  # noqa: BLE001 - report, keep walking
            w = DeliveryWalk(delivery=d, outcome="EXCEPTION", detail=f"{type(exc).__name__}: {exc}")
            walks.append(w)
            continue
        if not loads:
            walks.append(
                DeliveryWalk(delivery=d, outcome="NO_SHEET", detail="no variant selects a sheet")
            )
            continue
        for load in loads:
            dd = d if load.sheet is None else Delivery(**{**d.__dict__, "sheet": load.sheet})
            walks.append(walk_delivery(spec, dd, load))
            load.rows.clear()
            load.quarantined.clear()

    p("## Layer 1 - landed")
    p("")
    p(
        "| file | outcome | variant | lines after header | footer line | "
        "extra columns (kept as extra_cells) | optional columns absent | "
        "prohibited matched and dropped |"
    )
    p("|---|---|---|---:|---|---|---|---|")
    for w in walks:
        lines = w.rows_read + (1 if w.footer_line else 0)
        p(
            f"| {w.delivery.display_name} | {w.outcome} | {w.variant or '-'} | {lines:,} | "
            f"{w.footer_line or '-'} | {', '.join(w.new_columns) or '-'} | "
            f"{', '.join(w.missing_optional) or '-'} | {', '.join(w.prohibited_matched) or '-'} |"
        )
    bad = [w for w in walks if w.outcome not in ("LOADED",)]
    if bad:
        p("")
        p("Deliveries that did not load, with the loader's reason:")
        p("")
        for w in bad:
            p(f"- {w.delivery.display_name}: {w.outcome} - {w.detail}")
    p("")

    p("## Layer 2 - typed")
    p("")
    p("| file | read | typed | quarantined | balances | footer check | as-of floor |")
    p("|---|---:|---:|---:|---|---|---|")
    for w in walks:
        if w.outcome == "EXCEPTION" or w.outcome == "NO_SHEET":
            continue
        if w.footer_agrees is None:
            fc = "no footer"
        elif w.footer_agrees:
            fc = "agrees"
        else:
            fc = "MISMATCH " + "; ".join(
                f"{k}: declared {dv} computed {cv}" for k, (dv, cv) in w.footer_mismatches.items()
            )
        if spec.event_date_column is None:
            floor = "n/a"
        elif w.delivery.as_of is None:
            floor = "no as-of"
        else:
            floor = "VIOLATED " + w.floor_problem if w.floor_problem else "holds"
        p(
            f"| {w.delivery.display_name} | {w.rows_read:,} | {w.rows_ok:,} | "
            f"{w.rows_quarantined:,} | "
            f"{'yes' if w.balances else 'NO'} | {fc} | {floor} |"
        )
    reasons: Counter = Counter()
    for w in walks:
        reasons.update(w.quarantine_reasons)
    p("")
    if reasons:
        p("Quarantine reasons across all deliveries (column, reason, cells):")
        p("")
        p("| column | reason | cells |")
        p("|---|---|---:|")
        for (col, reason), n in reasons.most_common():
            p(f"| {col} | {reason} | {n:,} |")
    else:
        p("No row was quarantined in any delivery.")
    if controls:
        p("")
        p("Control totals per delivery (sum over typed rows):")
        p("")
        p("| file | " + " | ".join(controls) + " |")
        p("|---|" + "---:|" * len(controls))
        for w in walks:
            if not w.control_totals:
                continue
            p(
                f"| {w.delivery.display_name} | "
                + " | ".join(f"{w.control_totals.get(c, Decimal(0)):,}" for c in controls)
                + " |"
            )
    p("")

    # ---- Layer 3 -------------------------------------------------------
    p("## Layer 3 - curated (dedup rule applied in as-of order)")
    p("")
    loaded = [w for w in walks if w.outcome == "LOADED"]
    p(
        "Within-file duplicate identities (rows that share identity columns in one file, "
        "kept as occurrence 1, 2, 3 ...):"
    )
    p("")
    p(
        "| file | identity groups with >1 row | rows in them | "
        "groups byte-identical | max occurrence |"
    )
    p("|---|---:|---:|---:|---:|")
    for w in loaded:
        p(
            f"| {w.delivery.display_name} | {w.dup_groups:,} | {w.dup_rows:,} | "
            f"{w.dup_identical_groups:,} | {w.max_occurrence} |"
        )
    p("")
    refused = [w for w in loaded if w.delivery.as_of is None or w.floor_problem]
    if refused:
        p("Deliveries the curated layer refuses, so they are not applied below:")
        p("")
        for w in refused:
            why = w.delivery.as_of_problem or f"as-of floor: {w.floor_problem}"
            p(f"- {w.delivery.display_name}: {why}")
        p("")
    ordered = sorted(
        (w for w in loaded if w not in refused),
        key=lambda w: (w.delivery.as_of, w.delivery.display_name),
    )
    ledger = Ledger.for_spec(spec)
    p("Outcomes per delivery, in as-of order:")
    p("")
    p(
        "| order | file | as-of | coverage | inserted | unchanged | corrected | "
        "of which reorder only | late | conflict | vanished | current after |"
    )
    p("|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for i, w in enumerate(ordered, 1):
        o = ledger.apply(_facts(w))
        cov = str(w.coverage) if w.coverage else "unknown"
        held = " (HELD)" if o.held else ""
        p(
            f"| {i} | {w.delivery.display_name} | {w.delivery.as_of} | {cov} | "
            f"{o.inserted:,} | {o.unchanged:,} | {o.corrected:,} | {o.reordered:,} | "
            f"{o.late:,} | {o.conflict:,}{held} | {o.vanished:,} | {len(ledger.current):,} |"
        )
    p("")
    if ordered:
        oldest = ordered[0]
        before = ledger.snapshot()
        o = ledger.apply(_facts(oldest))
        after = ledger.snapshot()
        moved = sum(1 for k, v in after.items() if before.get(k) != v) + sum(
            1 for k in before if k not in after
        )
        p(
            f"Late-file test: replaying the oldest delivery ({oldest.delivery.display_name}, as-of "
            f"{oldest.delivery.as_of}) after everything else gives late {o.late:,}, unchanged "
            f"{o.unchanged:,}, inserted {o.inserted:,}, conflict {o.conflict:,}, vanished "
            f"{o.vanished:,}; current rows changed: {moved}."
        )
        p("")
    if spec.event_date is None:
        p(
            "Not simulated: the `vanished` outcome. The spec declares no event date, so "
            "nothing says which dates a newer delivery covers."
        )
    elif not spec.vanishes:
        p("The `vanished` outcome is switched off for this source (`vanish: false`).")
    else:
        p(
            "The `vanished` outcome ran: a current row is retired when a newer delivery "
            "covers its date"
            + (f" and its {', '.join(spec.vanish_scope)}" if spec.vanish_scope else "")
            + " and no longer carries its identity. A delivery whose coverage is unknown "
            "retires nothing."
        )
    p("")

    # ---- Layer 4 -------------------------------------------------------
    p("## Layer 4 - presented (current rows only)")
    p("")
    p(f"Current rows: {len(ledger.current):,}")
    if controls:
        sums = ledger.control_sums(len(controls))
        p("")
        p("| control column | sum over current rows |")
        p("|---|---:|")
        for c, s in zip(controls, sums, strict=True):
            p(f"| {c} | {s:,} |")
    by_delivery: Counter = Counter(cur.delivery for cur in ledger.current.values())
    p("")
    p("Current rows by the delivery that last set them:")
    p("")
    for name, n in by_delivery.most_common():
        p(f"- {name}: {n:,}")
    superseded: Counter = Counter(h.reason for h in ledger.history)
    if superseded:
        p("")
        p(
            "History rows (never current, or no longer current): "
            + ", ".join(f"{reason} {n:,}" for reason, n in sorted(superseded.items()))
            + "."
        )
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("key", nargs="?", help="spec key, e.g. wholesaler.mckesson.invoice")
    parser.add_argument("--all", action="store_true", help="walk every spec")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--out", type=Path, help="directory to write <key>.md into; stdout if omitted"
    )
    args = parser.parse_args()
    if not args.key and not args.all:
        parser.error("give a spec key or --all")

    registry = SpecRegistry.from_dir(REPO / "specs")
    keys = [s.key for s in registry] if args.all else [args.key]
    for key in keys:
        spec = registry.get(key)
        report = walk_source(spec, args.root)
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            target = args.out / f"{key}.md"
            target.write_text(report, encoding="utf-8")
            print(f"wrote {target}")
        else:
            print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
