"""Trace one vendor file through every stage and print what each produced.

    python scripts/trace_file.py wholesaler.mckesson.invoice "<path to one file>"

A reading aid, not a pipeline step. ``scripts/layer_walk.py`` reports a whole
source; this follows a single file and stops at each stage to say what the
code just decided and why, in the order the docs describe: delivery facts,
header binding, typing, footer, facts and the dedup rule against an empty
ledger. Read it beside ``docs/decision-record.md`` and the flow chart.

PHI: prints counts, column names, positions, outcomes and reason codes. It
never prints a cell value, a key value or a quarantine payload.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.curate.delivery import (  # noqa: E402
    AsOfUnavailableError,
    as_of_floor_violation,
    filename_window,
    resolve_as_of,
    resolve_coverage,
)
from sbh_data_bank.curate.rule import DeliveryFacts, Ledger, facts_for  # noqa: E402
from sbh_data_bank.ingest.load import load_file  # noqa: E402
from sbh_data_bank.registry.loader import SpecRegistry  # noqa: E402
from sbh_data_bank.store.schema import identity_columns_for  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def section(title: str) -> None:
    print()
    print(f"== {title}")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    key, path = sys.argv[1], Path(sys.argv[2])
    spec = SpecRegistry.from_dir(REPO / "specs").require(key)

    section("0. The spec")
    print(f"key            {spec.key}")
    print(f"variants       {[v.name for v in spec.variants]}")
    print(f"natural key    {spec.natural_key or '(none: every column is the identity)'}")
    print(f"event date     {spec.event_date}")
    print(
        f"coverage       {spec.coverage.value}  offsets {spec.coverage_start_offset_days:+d}/"
        f"{spec.coverage_end_offset_days:+d}"
    )
    print(f"vanish         {'on' if spec.vanishes else 'off'}  scope {spec.vanish_scope}")
    print(f"row hash over  {len(spec.row_hash_columns)} columns, excluding {spec.row_hash_exclude}")

    section("1. Delivery facts (curate/delivery.py)")
    print(f"file           {path.name}")
    print(f"filename says  {filename_window(path.name)}")
    try:
        as_of = resolve_as_of(path, spec, root=path.parent)
    except AsOfUnavailableError as exc:
        print(f"as-of          REFUSED: {exc}")
        return 1
    print(f"as-of          {as_of.value} from {as_of.source.value}")

    section("2. Bind and load (ingest/load.py -> binding.py, reader.py)")
    load = load_file(path, spec)
    print(f"outcome        {load.outcome}" + (f"  ({load.detail})" if load.detail else ""))
    if load.binding is None:
        return 1
    b = load.binding
    print(f"variant        {b.variant.name}")
    print(f"bound columns  {len(b.present)} of {len(b.columns)} spec columns matched a header")
    for cb in b.columns[:6]:
        pos = "-" if cb.match is None else cb.match.position
        print(f"               {cb.column.target:<34} <- position {pos}")
    if len(b.columns) > 6:
        print(f"               ... {len(b.columns) - 6} more")
    print(f"drift          {b.drift_report()}")
    print(f"prohibited     {[cb.column.target for cb in b.prohibited] or 'none'}")

    section("3. Typing (reader.type_row per row)")
    c = load.counters
    print(f"rows read      {c.rows_read}")
    print(f"typed          {c.rows_ok}")
    print(f"quarantined    {c.rows_quarantined}")
    print(f"balances       {c.balances}  (read == typed + quarantined)")
    if load.quarantined:
        reasons: dict[tuple[str, str], int] = {}
        for q in load.quarantined:
            for e in q.errors:
                reasons[(e.column, e.reason)] = reasons.get((e.column, e.reason), 0) + 1
        for (col, reason), n in sorted(reasons.items(), key=lambda kv: -kv[1])[:8]:
            print(f"               {col} {reason}: {n}")
    if load.rows:
        first = load.rows[0]
        print(
            f"first typed row line {first.line_number}: "
            f"{len(first.values)} values, types "
            f"{sorted({type(v).__name__ for v in first.values.values()})}"
        )
    for name, total in sorted(c.control_totals.items()):
        print(f"control total  {name:<34} {total:,}")

    section("4. Footer (load._looks_like_footer)")
    if load.footer is None:
        print("no footer recognised")
    else:
        print(f"footer line    {load.footer.line_number}")
        print(f"check          {load.footer.describe()}")

    if not load.ok:
        print()
        print(f"stopping: the load is not ok ({load.outcome}); nothing would commit")
        return 1

    section("5. Facts for the curated layer (curate/rule.py facts_for)")
    identity = identity_columns_for(spec)
    controls = sorted(c.control_totals)
    facts = facts_for(spec, load.rows, as_of=as_of.value, controls=controls)
    print(f"identity       {list(identity)} + occurrence")
    print(f"facts          {len(facts)}")
    print(f"max occurrence {max((f.occurrence for f in facts), default=0)}")
    collisions = sum(1 for f in facts if f.occurrence > 1)
    print(f"rows sharing an identity with an earlier row in this file: {collisions}")
    if spec.event_date_column:
        dates = [r.values.get(spec.event_date_column) for r in load.rows]
        floor = as_of_floor_violation(as_of.value, dates)
        print(f"as-of floor    {'VIOLATED: ' + floor if floor else 'holds'}")
    cov_col = spec.coverage_target
    coverage = resolve_coverage(
        spec,
        filename=path.name,
        as_of=as_of.value,
        coverage_dates=(r.values.get(cov_col) for r in load.rows) if cov_col else (),
    )
    print(f"coverage       {coverage if coverage else 'unknown (vanish would not run)'}")
    if spec.vanish_scope:
        print(f"scope values   {len({f.scope for f in facts})} distinct {spec.vanish_scope}")

    section("6. The dedup rule against an empty ledger (Ledger.apply)")
    ledger = Ledger.for_spec(spec)
    out = ledger.apply(DeliveryFacts(path.name, as_of.value, coverage, facts))
    print(
        f"inserted {out.inserted}  unchanged {out.unchanged}  corrected {out.corrected}  "
        f"late {out.late}  conflict {out.conflict}  vanished {out.vanished}"
    )
    print(f"current rows   {len(ledger.current)}")
    print()
    print("Against a ledger that already holds earlier deliveries, the same rows would be")
    print("unchanged, corrected, late or conflict instead. scripts/layer_walk.py shows that.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
