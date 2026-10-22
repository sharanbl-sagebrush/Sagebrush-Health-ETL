"""Close ``meta.run`` rows a process left open.

    python scripts/close_runs.py --env-file .env
    python scripts/close_runs.py --env-file .env --apply --actor <you>

A run groups the batches of one invocation and is closed by the process
that opened it. When that process ends first (the connection dropped, the
window was shut, the machine slept) the run stays open with no outcome
while every batch it wrote is complete, because each file commits in its
own transaction. Such a run is not a failure and not in progress; it is
interrupted, and this records it as ``INTERRUPTED`` with what its batches
did, so the freshness check stops listing it.

Without ``--apply`` the script only lists what it would close. A run
younger than ``--older-than-hours`` (default 6) is left alone, since it may
still be running.

PHI: prints run ids, spec keys, timestamps, outcomes and counts.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.commit import SqlStore  # noqa: E402
from sbh_data_bank.commit.records import RunRecord  # noqa: E402
from sbh_data_bank.store.config import SqlSettings, load_env_file  # noqa: E402
from sbh_data_bank.store.connection import Database  # noqa: E402

OPEN_RUNS = (
    "SELECT run_id, tenant_id, trigger_kind, started_at FROM meta.run "
    "WHERE tenant_id = ? AND finished_at IS NULL AND outcome IS NULL AND started_at < ? "
    "ORDER BY started_at"
)
RUN_BATCHES = (
    "SELECT source_key, outcome, COUNT(*), MAX(finished_at) FROM meta.batch "
    "WHERE run_id = ? GROUP BY source_key, outcome"
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--older-than-hours", type=int, default=6)
    parser.add_argument("--actor")
    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)
    settings = SqlSettings.from_env()
    actor = args.actor or settings.user or getpass.getuser()
    cutoff = datetime.now() - timedelta(hours=args.older_than_hours)
    print(f"open runs on {settings.describe()} started before {cutoff:%Y-%m-%d %H:%M}")
    with Database(settings) as db:
        runs = db.execute(OPEN_RUNS, settings.tenant, cutoff)
        if not runs:
            print("  none")
            return 0
        store = SqlStore(db)
        for run_id, tenant_id, trigger_kind, started_at in runs:
            batches = db.execute(RUN_BATCHES, run_id)
            counts = Counter()
            sources = set()
            last: datetime | None = None
            for source_key, outcome, n, finished in batches:
                counts[outcome] += int(n)
                sources.add(source_key)
                if finished is not None and (last is None or finished > last):
                    last = finished
            done = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no batches"
            print(f"  {run_id} ({trigger_kind}) started {started_at:%Y-%m-%d %H:%M}: {done}")
            for key in sorted(sources):
                print(f"      {key}")
            if not args.apply:
                continue
            record = RunRecord(
                run_id=run_id,
                tenant_id=tenant_id,
                trigger_kind=trigger_kind,
                started_at=started_at,
                finished_at=last or datetime.now(),
                outcome="INTERRUPTED",
                detail=(
                    f"closed by {actor} on {datetime.now():%Y-%m-%d}: the process ended before "
                    f"the run was closed; its batches had finished ({done})"
                ),
            )
            with store.transaction():
                store.finish_run(record)
            print("      -> INTERRUPTED")
    if not args.apply:
        print("\nnothing changed; add --apply to close them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
