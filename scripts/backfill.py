"""Commit every delivery of every source, in as-of order, through the engine.

    python scripts/backfill.py --dry-run
    python scripts/backfill.py --env-file .env
    python scripts/backfill.py --env-file .env --only wholesaler.mckesson.invoice

For each spec, in the load order the layer walk settled on, the script finds
every file under the root whose name the spec accepts (zip members
included), orders them by as-of (decision D5) and commits them one at a
time. The engine decides each file's outcome; nothing here overrides it. A
file the engine refuses is listed with its reason and the next file is
tried, because a refusal leaves only the file and batch records and the
ordering of what follows does not depend on it. A store failure (an
exception, as opposed to a refusal) stops that source and the run is marked
FAILED, so the operator sees it; other sources continue.

The whole thing is idempotent: a file already committed is recorded as
``DUPLICATE_FILE`` and writes nothing, so re-running after adding files to
the root commits only what is new.

The run ends with the files under the root that no spec claims. Discovery
is by filename pattern, so a vendor file renamed past every pattern is
never visited; this list is the only place such a file shows up.

Options
    --dry-run           run everything against an in-memory store; prints
                        the same report, touches nothing
    --env-file <path>   the ``SBH_SQL_*`` settings for a real run
    --root <dir>        where the vendor files are; defaults to
                        ``SBH_SOURCE_ROOT`` or the OneDrive folder
    --only <key>...     these specs only; --skip <key>... all but these
    --operator-date     an as-of for files that have no other source of one
    --actor <name>      ``created_by`` on every row
    --fast              batched inserts (pyodbc fast_executemany with every
                        parameter declared from the DDL); one round trip per
                        batch instead of one per row

PHI: prints filenames, counts, outcomes and reason text. Never a cell value.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.commit import (  # noqa: E402
    CommitEngine,
    CommitResult,
    MemoryStore,
    SqlStore,
    discover,
    in_as_of_order,
    unclaimed,
)
from sbh_data_bank.commit.records import Store  # noqa: E402
from sbh_data_bank.registry.loader import SpecRegistry  # noqa: E402
from sbh_data_bank.store.config import SqlSettings, load_env_file  # noqa: E402
from sbh_data_bank.store.connection import Database, is_transient  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path(
    os.environ.get(
        "SBH_SOURCE_ROOT",
        r"C:\Users\SharanLokeshwar\OneDrive - Sagebrush Healthcare\Sagebrush ETL Files",
    )
)

#: The order the layer walk settled on (docs/layer-walk/README.md): the
#: sources whose rule is proven first, the reference data last. Specs not
#: named here follow in registry order.
LOAD_ORDER = [
    "wholesaler.mckesson.invoice",
    "wholesaler.cardinal.invoice",
    "wholesaler.cardinal.invoice_summary",
    "wholesaler.abc.purchases",
    "tpa.wellpartner.invoices",
    "tpa.pharmaforce.claims",
    "ehr.athena.encounters",
    "ehr.athena.prescriptions",
    "ehr.athena.eligibility",
    "ehr.allscripts.erx",
    "reference.wholesaler_accounts",
    "dispense.weinfuse.administered",
    "dispense.weinfuse.patients",
    "tpa.prxp.transactions",
    "ehr.curve.monthly_services",
    "ehr.curve.appointments",
    "finance.monthly_payments",
    "tpa.wellpartner.claims",
    "wholesaler.mckesson.custom",
    "dispense.weinfuse.buybill",
]


#: Outcomes that write nothing and need nobody's attention: the same bytes
#: again, or a sheet the spec recognises and deliberately does not load.
#: Every other non-LOADED outcome is a refusal, and the run says INCOMPLETE.
BENIGN = frozenset({"DUPLICATE_FILE", "RECOGNISED_NOT_LOADED"})


def _ordered_specs(registry: SpecRegistry, only: list[str], skip: list[str]):
    rank = {key: i for i, key in enumerate(LOAD_ORDER)}
    specs = sorted(registry, key=lambda s: (rank.get(s.key, len(rank)), s.key))
    if only:
        specs = [s for s in specs if s.key in set(only)]
    if skip:
        specs = [s for s in specs if s.key not in set(skip)]
    return specs


def _counters(results: list[CommitResult]) -> Counter:
    total: Counter = Counter()
    for r in results:
        total[r.outcome] += 1
        total["rows_read"] += r.rows_read
        total["quarantined"] += r.quarantined
        if r.outcomes is not None:
            for name in ("inserted", "unchanged", "corrected", "late", "conflict", "vanished"):
                total[name] += getattr(r.outcomes, name)
    return total


def backfill(
    store: Store,
    registry: SpecRegistry,
    root: Path,
    *,
    tenant_id: str,
    actor: str,
    only: list[str],
    skip: list[str],
    operator_date: date | None,
    reconnect: Callable[[], None] | None = None,
) -> dict[str, Counter]:
    """Commit every source's deliveries in order; returns a counter per spec.

    A transient store fault (the link dropped, a deadlock, a timeout) gets
    one retry of the same file after ``reconnect`` is called; the file's
    transaction rolled back, so the retry starts clean. Anything else, or a
    second fault, marks the run FAILED and moves to the next source.
    """
    engine = CommitEngine(store, tenant_id=tenant_id, actor=actor)
    report: dict[str, Counter] = {}
    for spec in _ordered_specs(registry, only, skip):
        found = discover(spec, root)
        ordered = in_as_of_order(spec, found, root, operator_date=operator_date)
        print(f"\n=== {spec.key}: {len(ordered)} deliveries")
        run = engine.begin_run("backfill")
        results: list[CommitResult] = []
        started = time.monotonic()
        failed: Exception | None = None
        for as_of, item in ordered:
            when = as_of.value.isoformat() if as_of else "no as-of "
            for attempt in (1, 2):
                try:
                    for r in engine.commit_path(
                        spec,
                        item.path,
                        run_id=run.run_id,
                        root=root,
                        operator_date=operator_date,
                        member=item.member,
                    ):
                        results.append(r)
                        print(f"  {when}  {r.summary()}")
                    break
                except Exception as exc:  # noqa: BLE001 - decide retry or record
                    if attempt == 1 and reconnect is not None and is_transient(exc):
                        print(
                            f"  {when}  {item.display}: transient fault, reconnecting and retrying"
                        )
                        reconnect()
                        continue
                    failed = exc
                    break
            if failed is not None:
                break
        if failed is not None:
            print(f"  FAILED after {len(results)} files: {type(failed).__name__}: {failed}")
            try:
                engine.finish_run(run, "FAILED", f"{type(failed).__name__}: {failed}"[:2000])
            except Exception as exc:  # noqa: BLE001 - the store is what failed
                print(f"  (could not record the failed run: {type(exc).__name__}: {exc})")
            report[spec.key] = _counters(results) + Counter(FAILED=1)
            continue
        counters = _counters(results)
        outcome = (
            "LOADED" if all(r.committed or r.outcome in BENIGN for r in results) else "INCOMPLETE"
        )
        engine.finish_run(run, outcome)
        print(
            f"  -> {outcome} in {time.monotonic() - started:.0f}s: "
            + ", ".join(f"{k}={v}" for k, v in sorted(counters.items()))
        )
        report[spec.key] = counters
    return report


def _print_unclaimed(registry: SpecRegistry, root: Path) -> None:
    orphans = unclaimed(registry, root)
    print(f"\nfiles under the root that no spec claims: {len(orphans)}")
    for rel in orphans:
        print(f"  {rel.as_posix()}")


def _print_report(report: dict[str, Counter]) -> None:
    print("\n" + "=" * 100)
    head = f"{'source':<34}{'LOADED':>7}{'dup':>5}{'refused':>8}{'inserted':>10}{'unchg':>7}"
    head += f"{'corr':>6}{'late':>6}{'conf':>6}{'vanish':>7}{'quar':>6}"
    print(head)
    for key, c in report.items():
        loaded = c.get("LOADED", 0)
        benign = sum(v for k, v in c.items() if k in BENIGN)
        refused = sum(v for k, v in c.items() if k.isupper() and k != "LOADED" and k not in BENIGN)
        line = f"{key:<34}{loaded:>7}{benign:>5}{refused:>8}{c.get('inserted', 0):>10}"
        line += f"{c.get('unchanged', 0):>7}{c.get('corrected', 0):>6}{c.get('late', 0):>6}"
        line += f"{c.get('conflict', 0):>6}{c.get('vanished', 0):>7}{c.get('quarantined', 0):>6}"
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--skip", nargs="*", default=[])
    parser.add_argument("--operator-date", type=date.fromisoformat)
    parser.add_argument("--actor")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--specs", type=Path, default=REPO / "specs")
    args = parser.parse_args()

    registry = SpecRegistry.from_dir(args.specs)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"root is not a directory: {root}")
        return 2

    if args.dry_run:
        print(f"dry run over {root}")
        report = backfill(
            MemoryStore(),
            registry,
            root,
            tenant_id="dry-run",
            actor=args.actor or getpass.getuser(),
            only=args.only,
            skip=args.skip,
            operator_date=args.operator_date,
        )
        _print_report(report)
        _print_unclaimed(registry, root)
        return 0

    if args.env_file:
        load_env_file(args.env_file)
    settings = SqlSettings.from_env()
    print(f"backfilling {settings.describe()} from {root}")
    with Database(settings) as database:
        report = backfill(
            SqlStore(database, fast_executemany=args.fast),
            registry,
            root,
            tenant_id=settings.tenant,
            actor=args.actor or settings.user or getpass.getuser(),
            only=args.only,
            skip=args.skip,
            operator_date=args.operator_date,
            reconnect=database.close,
        )
    _print_report(report)
    _print_unclaimed(registry, root)
    return 0 if not any("FAILED" in c for c in report.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
