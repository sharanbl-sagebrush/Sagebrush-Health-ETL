"""Type every loaded file again from the landed layer and report what would change.

    python scripts/retype.py --env-file .env --only wholesaler.mckesson.invoice
    python scripts/retype.py --env-file .env

For each file a source has loaded, the landed rows and the recorded
header are read back from the database and typed under the spec as it is
now (``ingest.replay``), without the file. Each line is then compared with
what the original load wrote: the same row hash (``unchanged``), a
different one (``rehashed``), typed then but quarantined now, or the
reverse; and the replay's control totals are held against the batch's.
Under an unchanged spec every line is unchanged, which is the proof that
the landed layer holds everything typing needs.

Reports, and records its verdict. Writing the rebuilt typed rows and
re-curating from them is decision D18, not yet taken, so there is no
``--apply``; the one row written is ``meta.spec_check`` per source (the
spec digest and how many files would change), which is what clears the
freshness check's ``SPEC_CHANGED`` once a spec edit is shown to change
nothing. The exit code is 1 when any file would change, so the script
doubles as the check to run after editing a spec: it says, file by
file, what the edit does to data already loaded, before any file is read
again.

Options
    --env-file <path>   the ``SBH_SQL_*`` settings
    --only <key>...     these specs only; --skip <key>... all but these
    --verbose           one line per file, not only the files that differ
    --actor <name>      ``checked_by`` on the spec_check row

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

from sbh_data_bank.commit import SqlStore, replay_source  # noqa: E402
from sbh_data_bank.commit.retype import record_check  # noqa: E402
from sbh_data_bank.registry.loader import SpecRegistry  # noqa: E402
from sbh_data_bank.store.config import SqlSettings, load_env_file  # noqa: E402
from sbh_data_bank.store.connection import Database  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--skip", nargs="*", default=[])
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--actor")
    parser.add_argument("--specs", type=Path, default=REPO / "specs")
    args = parser.parse_args()

    registry = SpecRegistry.from_dir(args.specs)
    specs = [
        s
        for s in registry
        if (not args.only or s.key in set(args.only)) and s.key not in set(args.skip)
    ]
    if args.env_file:
        load_env_file(args.env_file)
    settings = SqlSettings.from_env()
    actor = args.actor or settings.user or getpass.getuser()
    print(f"retyping from the landed layer of {settings.describe()}")

    differ = 0
    with Database(settings) as db:
        store = SqlStore(db)
        for spec in specs:
            started = time.monotonic()
            replays = []
            for replay in replay_source(store, spec, settings.tenant):
                replays.append(replay)
                if args.verbose or not replay.identical:
                    print(f"  {replay.summary()}")
            changed = sum(1 for r in replays if not r.identical)
            differ += changed
            record_check(db, spec, replays, tenant_id=settings.tenant, actor=actor)
            print(
                f"=== {spec.key}: {len(replays)} files, {sum(r.lines for r in replays)} lines, "
                f"{changed} would change ({time.monotonic() - started:.0f}s)"
            )
    print(f"\nfiles that would change: {differ}")
    return 1 if differ else 0


if __name__ == "__main__":
    raise SystemExit(main())
