"""Commit one vendor file through the layers.

    python scripts/commit_file.py <spec key> "<path>" --dry-run
    python scripts/commit_file.py <spec key> "<path>" --env-file <path to env>

``--dry-run`` runs the whole engine against an in-memory store and prints
what would be written: the batch outcome, the rule's counters and a row
count per table. Nothing touches Azure. Without it, the file is committed
to the database named by ``SBH_SQL_*`` in one transaction, under a
``meta.run`` with trigger ``manual``.

Options
    --root <dir>            bounds the dated-folder search for the as-of
                            (decision D5) and labels the delivery by the
                            file's folder. Defaults to the file's parent.
    --operator-date <date>  the as-of to use when nothing else yields one.
    --actor <name>          ``created_by`` on every row. Defaults to the
                            SQL login, or the OS user for a dry run.
    --replay                derive the batch id from the file's content, so
                            a replay of the same file is byte-identical.

PHI: prints counts, outcomes and reason text. Never a cell value.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.commit import CommitEngine, MemoryStore, SqlStore  # noqa: E402
from sbh_data_bank.registry.loader import SpecRegistry  # noqa: E402
from sbh_data_bank.store.config import SqlSettings, load_env_file  # noqa: E402
from sbh_data_bank.store.connection import Database  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec_key")
    parser.add_argument("path", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--operator-date", type=date.fromisoformat)
    parser.add_argument("--actor")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--specs", type=Path, default=REPO / "specs")
    args = parser.parse_args()

    spec = SpecRegistry.from_dir(args.specs).require(args.spec_key)
    path: Path = args.path.resolve()
    root: Path = (args.root or path.parent).resolve()

    if args.dry_run:
        store = MemoryStore()
        engine = CommitEngine(
            store,
            tenant_id="dry-run",
            actor=args.actor or getpass.getuser(),
            deterministic_ids=args.replay,
        )
        run = engine.begin_run("manual")
        results = engine.commit_path(
            spec, path, run_id=run.run_id, root=root, operator_date=args.operator_date
        )
        for r in results:
            print(r.summary())
        print()
        print("would write:")
        print(f"  meta.source_file      {len(store.source_files)}")
        print(f"  meta.batch            {len(store.batches)}")
        print(f"  landed                {len(store.landed[spec.key])}")
        print(f"  typed                 {len(store.typed[spec.key])}")
        print(f"  curated               {len(store.curated[spec.key])}")
        print(f"  stg.quarantine        {len(store.quarantine)}")
        print(f"  stg.duplicate_finding {len(store.findings)}")
        return 0 if all(r.committed for r in results) else 1

    if args.env_file:
        load_env_file(args.env_file)
    settings = SqlSettings.from_env()
    actor = args.actor or settings.user or getpass.getuser()
    print(f"committing to {settings.describe()}")
    with Database(settings) as database:
        engine = CommitEngine(
            SqlStore(database),
            tenant_id=settings.tenant,
            actor=actor,
            deterministic_ids=args.replay,
        )
        run = engine.begin_run("manual")
        try:
            results = engine.commit_path(
                spec, path, run_id=run.run_id, root=root, operator_date=args.operator_date
            )
        except Exception as exc:  # noqa: BLE001 - the run record must say it failed
            engine.finish_run(run, "FAILED", str(exc)[:2000])
            raise
        outcome = "LOADED" if all(r.committed for r in results) else results[-1].outcome
        engine.finish_run(run, outcome)
    for r in results:
        print(r.summary())
    return 0 if all(r.committed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
