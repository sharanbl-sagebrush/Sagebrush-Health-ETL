"""Rebuild typed and curated from the landed layer under the spec as it is now.

    python scripts/rebuild.py --env-file .env --only finance.monthly_payments
    python scripts/rebuild.py --env-file .env --only finance.monthly_payments --apply

Decision D18 (2026-11-03): a rule change on a loaded source is applied
inside the database. Every loaded file is typed again from ``landed``
(what ``retype.py`` does), the typed rows are replaced, and the live
curated rows are reconciled with what the dedup rule gives under the new
spec: rows it still produces are kept, every other live row is retired
with reason ``retyped``, and the rows it newly produces are inserted.
One transaction per source; a refusal (a file the spec no longer loads,
a line that would newly quarantine or newly type, control totals that
disagree, a conflict in the replay) rolls the source back untouched.

Without ``--apply`` nothing is written and the same counts are printed:
read them first. With ``--apply`` the rebuild batch is written, the
``meta.spec_check`` verdict is recorded for the new digest (so the
freshness check stops raising ``SPEC_CHANGED``), and the batch id is
printed. The exit code is 1 when any source was refused.

Options
    --env-file <path>   the ``SBH_SQL_*`` settings
    --only <key>...     these specs only (required: a rebuild is deliberate)
    --apply             write; without it, report
    --actor <name>      ``created_by`` on every row the rebuild writes
    --verbose           one line per file, not only the files that differ

PHI: prints filenames, counts, line numbers and reason codes. Never a
cell value.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.commit import CommitEngine, SqlStore, rebuild_source  # noqa: E402
from sbh_data_bank.commit.retype import SPEC_CHECK_INSERT  # noqa: E402
from sbh_data_bank.registry.loader import SpecRegistry  # noqa: E402
from sbh_data_bank.store.config import SqlSettings, load_env_file  # noqa: E402
from sbh_data_bank.store.connection import Database  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--only", nargs="+", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--actor")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--specs", type=Path, default=REPO / "specs")
    args = parser.parse_args()

    registry = SpecRegistry.from_dir(args.specs)
    specs = [registry.require(key) for key in args.only]
    if args.env_file:
        load_env_file(args.env_file)
    settings = SqlSettings.from_env()
    actor = args.actor or settings.user or getpass.getuser()
    mode = "REBUILDING" if args.apply else "planning a rebuild of"
    print(f"{mode} {', '.join(s.key for s in specs)} in {settings.describe()}")

    refused = 0
    with Database(settings) as db:
        store = SqlStore(db, fast_executemany=True)
        engine = CommitEngine(store, tenant_id=settings.tenant, actor=actor)
        run = engine.begin_run("rebuild" if args.apply else "rebuild-plan")
        try:
            for spec in specs:
                started = time.monotonic()
                print(f"=== {spec.key}")

                def on_file(replay, verbose=args.verbose):
                    if verbose or not replay.identical:
                        print(f"  {replay.summary()}")

                result = rebuild_source(
                    store,
                    spec,
                    tenant_id=settings.tenant,
                    actor=actor,
                    run_id=run.run_id,
                    apply=args.apply,
                    on_file=on_file,
                )
                print(f"  -> {result.summary()} ({time.monotonic() - started:.0f}s)")
                if result.refused:
                    refused += 1
                elif args.apply:
                    db.execute(
                        SPEC_CHECK_INSERT,
                        settings.tenant, spec.key, result.spec_sha256, engine._clock(), actor,
                        result.files, result.lines, 0,
                    )  # fmt: skip
        finally:
            engine.finish_run(run, "REFUSED" if refused else "COMPLETED")
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
