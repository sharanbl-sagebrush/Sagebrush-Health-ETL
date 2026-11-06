"""Bring a source's superseded curated rows back in step with their typed
lines: every business column and the row hash.

    python scripts/refresh_history.py --env-file .env --only <spec key>          # count
    python scripts/refresh_history.py --env-file .env --only <spec key> --apply  # write

A curated row of the current generation must equal its typed line in
every business column and in the hash, history rows included. The
rebuild now keeps that true (``commit/rebuild.py``: the history refresh
after the reconcile); rebuilds before 2026-11-06 did not, and left
Curve services with 109,190 history rows carrying the old hash of their
refilled values and PRxP with 1,482 carrying nulls where the five newly
claimed columns had been typed. This script is the one-time repair for
those rows. It selects a superseded, non-retyped row when its hash or
any business column differs from typed (the rebuild selects by the
hash alone, which its plan can predict), changes no live row's standing,
and refuses if a live row were out of step, since that is a rebuild's
job and not this one's.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.commit.sql import refresh_history_sql  # noqa: E402
from sbh_data_bank.registry.loader import SpecRegistry  # noqa: E402
from sbh_data_bank.store.config import SqlSettings, load_env_file  # noqa: E402
from sbh_data_bank.store.connection import Database  # noqa: E402
from sbh_data_bank.store.schema import table_name  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--only", nargs="+", required=True)
    parser.add_argument("--specs", type=Path, default=REPO / "specs")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    registry = SpecRegistry.from_dir(args.specs)
    if args.env_file:
        load_env_file(args.env_file)
    settings = SqlSettings.from_env()
    with Database(settings) as db:
        db.connection.timeout = 3600
        cur = db.connection.cursor()
        for key in args.only:
            spec = registry.require(key)
            curated, typed = table_name(spec, "curated"), table_name(spec, "typed")
            cur.execute(
                refresh_history_sql(spec, count_only=True, any_column=True), settings.tenant
            )
            (stale,) = cur.fetchone()
            cur.execute(
                f"SELECT COUNT(*) FROM {curated} AS c JOIN {typed} AS ty "
                f"ON ty.source_file_id = c.source_file_id AND ty.line_number = c.line_number "
                f"WHERE c.tenant_id = ? AND c.superseded_by IS NULL AND c.row_hash <> ty.row_hash",
                settings.tenant,
            )
            (live,) = cur.fetchone()
            print(
                f"{key}: {stale} history rows differ from their typed line; "
                f"{live} live rows do (must be 0)"
            )
            if live:
                print(f"{key}: REFUSED, a live row out of step is a rebuild's job, not this one's")
                return 1
            if not stale or not args.apply:
                continue
            cur.execute(
                refresh_history_sql(spec, count_only=False, any_column=True), settings.tenant
            )
            print(f"{key}: {cur.rowcount} rows refreshed")
            db.connection.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
