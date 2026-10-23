"""Commit every delivery of every source, in as-of order, through the engine.

    python scripts/backfill.py --dry-run
    python scripts/backfill.py --env-file .env
    python scripts/backfill.py --env-file .env --only wholesaler.mckesson.invoice

The loop itself is ``commit.backfill`` (shared with the acquire step):
for each spec, in the load order the layer walk settled on, every file
under the root whose name the spec accepts is ordered by as-of (D5) and
committed one at a time; the engine decides each outcome. A refusal is
listed and the next file is tried; a store failure marks that source's
run FAILED and the next source continues. A file already committed is a
``DUPLICATE_FILE``, so re-running after adding files commits only what is
new.

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
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.commit import MemoryStore, SqlStore  # noqa: E402
from sbh_data_bank.commit.backfill import backfill, print_report, print_unclaimed  # noqa: E402
from sbh_data_bank.registry.loader import SpecRegistry  # noqa: E402
from sbh_data_bank.store.config import SqlSettings, load_env_file  # noqa: E402
from sbh_data_bank.store.connection import Database  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path(
    os.environ.get(
        "SBH_SOURCE_ROOT",
        r"C:\Users\SharanLokeshwar\OneDrive - Sagebrush Healthcare\Sagebrush ETL Files",
    )
)


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
        print_report(report)
        print_unclaimed(registry, root)
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
    print_report(report)
    print_unclaimed(registry, root)
    return 0 if not any("FAILED" in c for c in report.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
