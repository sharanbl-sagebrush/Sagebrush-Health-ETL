"""Is every source current, and is anything waiting for a person?

    python scripts/freshness.py --env-file .env
    python scripts/freshness.py --env-file .env --today 2026-11-03 --grace-days 2

One line per spec: its cadence, the as-of of its newest loaded delivery
and how many days ago that was, how many files have loaded, when the
pipeline last saw any file for it, and its open refusals and unresolved
quarantine. Then the findings (``observe.freshness``): STALE,
NEVER_LOADED, OPEN_REFUSAL, RUN_FAILED and RUN_UNFINISHED are alerts and
make the exit code 1; OPEN_QUARANTINE is a note. Nothing is written, so
this is safe to run from a schedule; the exit code is the alert.

``expected_cadence_days`` is a staleness threshold, not a schedule, and
``--grace-days`` (default 2) is what keeps a Sunday-morning check from
firing before the week's upload. ``--today`` measures staleness from
another day, for "what would have fired last Tuesday".

PHI: prints spec keys, filenames, dates, counts and outcomes. Never a
cell value.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.observe.freshness import (  # noqa: E402
    DEFAULT_GRACE_DAYS,
    DEFAULT_UNFINISHED_HOURS,
    DEFAULT_WINDOW_DAYS,
    SourceStatus,
    assess,
    stale_after,
)
from sbh_data_bank.observe.sql import collect  # noqa: E402
from sbh_data_bank.registry.loader import SpecRegistry  # noqa: E402
from sbh_data_bank.store.config import SqlSettings, load_env_file  # noqa: E402
from sbh_data_bank.store.connection import Database  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    parser.add_argument("--grace-days", type=int, default=DEFAULT_GRACE_DAYS)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--unfinished-hours", type=int, default=DEFAULT_UNFINISHED_HOURS)
    parser.add_argument("--specs", type=Path, default=REPO / "specs")
    args = parser.parse_args()

    registry = SpecRegistry.from_dir(args.specs)
    if args.env_file:
        load_env_file(args.env_file)
    settings = SqlSettings.from_env()
    now = datetime.now()
    if args.today != date.today():
        now = datetime(args.today.year, args.today.month, args.today.day, 12)
    print(f"freshness of {settings.describe()} as of {args.today}")
    with Database(settings) as db:
        statuses, runs = collect(db, settings.tenant, since=now - timedelta(days=args.window_days))

    head = f"{'source':<36}{'cadence':>8}{'latest as-of':>14}{'days':>6}{'files':>6}"
    head += f"{'last seen':>18}{'refused':>8}{'quar':>6}"
    print("\n" + head)
    for spec in registry:
        s = statuses.get(spec.key, SourceStatus(spec.key))
        limit = stale_after(spec, args.grace_days)
        cadence = "-" if spec.expected_cadence_days is None else str(spec.expected_cadence_days)
        if not spec.is_expected_to_deliver:
            cadence = spec.lifecycle.value
        as_of = s.latest_loaded_as_of.isoformat() if s.latest_loaded_as_of else "-"
        days = str((args.today - s.latest_loaded_as_of).days) if s.latest_loaded_as_of else "-"
        seen = s.latest_seen_at.strftime("%Y-%m-%d %H:%M") if s.latest_seen_at else "-"
        flag = " *" if limit is not None and s.latest_loaded_as_of and int(days) >= limit else ""
        print(
            f"{spec.key:<36}{cadence:>8}{as_of:>14}{days:>6}{s.loaded_files:>6}{seen:>18}"
            f"{len(s.open_refusals):>8}{s.open_quarantine:>6}{flag}"
        )

    findings = assess(
        list(registry),
        statuses,
        runs,
        today=args.today,
        now=now,
        grace_days=args.grace_days,
        window_days=args.window_days,
        unfinished_hours=args.unfinished_hours,
    )
    alerts = [f for f in findings if f.is_alert]
    print(f"\nfindings: {len(findings)} ({len(alerts)} alerts)")
    for f in findings:
        print(f"  {f}")
    return 1 if alerts else 0


if __name__ == "__main__":
    raise SystemExit(main())
