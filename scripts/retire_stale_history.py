"""Retire the history rows a committed rebuild left beside the current
row of their own line.

    python scripts/retire_stale_history.py --env-file .env --only <spec key>          # count
    python scripts/retire_stale_history.py --env-file .env --only <spec key> --apply  # write

A rebuild makes a line current when the new rule says so. If that line
already had a history row (superseded as ``corrected``, say, by a
correction the new rule no longer sees), the line then stands in the
current generation twice. The rebuild now retires such a row itself
(``commit/rebuild.py``, ``history_retired``); the Monthly Payments ``dob``
rebuild of 2026-11-11 ran before it did and left 64,673. This script is
the one-time repair for those rows: each is marked ``retyped`` and pointed
at the source's latest rebuild batch, exactly as the rebuild would have
written it, with today's time and the operator on the row.

It changes no live row. It refuses when the source has no rebuild batch,
and when a selected row's hash differs from the live row on its line:
after a rebuild both equal the typed line's, so a difference is something
this script does not understand.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.commit.rebuild import REBUILT, RETYPED  # noqa: E402
from sbh_data_bank.commit.sql import stale_history_sql  # noqa: E402
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
    parser.add_argument("--actor")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    registry = SpecRegistry.from_dir(args.specs)
    if args.env_file:
        load_env_file(args.env_file)
    settings = SqlSettings.from_env()
    actor = args.actor or settings.user or getpass.getuser()
    with Database(settings) as db:
        db.connection.timeout = 3600
        cur = db.connection.cursor()
        for key in args.only:
            spec = registry.require(key)
            curated = table_name(spec, "curated")
            cur.execute(stale_history_sql(spec, count_only=True), settings.tenant, RETYPED)
            (stale,) = cur.fetchone()
            print(f"{key}: {stale} history rows stand beside the current row of their own line")
            if not stale:
                continue
            cur.execute(
                f"SELECT c.supersede_reason, c.superseded_by, COUNT(*), "
                f"SUM(CASE WHEN l.row_hash = c.row_hash THEN 0 ELSE 1 END) "
                f"FROM {curated} AS c JOIN {curated} AS l ON l.tenant_id = c.tenant_id "
                f"AND l.source_file_id = c.source_file_id AND l.line_number = c.line_number "
                f"AND l.superseded_by IS NULL WHERE c.tenant_id = ? "
                f"AND c.superseded_by IS NOT NULL AND c.supersede_reason <> ? "
                f"GROUP BY c.supersede_reason, c.superseded_by ORDER BY c.superseded_by",
                settings.tenant,
                RETYPED,
            )
            differing = 0
            for reason, batch, rows, differ in cur.fetchall():
                differing += int(differ)
                print(
                    f"  {rows} superseded as {reason} by {batch}; {differ} differ from the live row"
                )
            if differing:
                print(f"{key}: REFUSED, {differing} rows do not carry the hash of their live row")
                return 1
            cur.execute(
                "SELECT TOP 1 batch_id FROM meta.batch WHERE tenant_id = ? AND source_key = ? "
                "AND outcome = ? ORDER BY started_at DESC",
                settings.tenant,
                key,
                REBUILT,
            )
            found = cur.fetchone()
            if found is None:
                print(f"{key}: REFUSED, the source has no rebuild batch to point the rows at")
                return 1
            (rebuild,) = found
            print(f"  the rebuild they belong to: {rebuild}")
            if not args.apply:
                continue
            now = datetime.now(UTC).replace(tzinfo=None)
            cur.execute(
                stale_history_sql(spec, count_only=False),
                rebuild, now, RETYPED, now, actor, settings.tenant, RETYPED,
            )  # fmt: skip
            written = cur.rowcount
            if written != stale:
                db.connection.rollback()
                print(f"{key}: REFUSED, the update touched {written} rows, not {stale}")
                return 1
            db.connection.commit()
            print(f"{key}: {written} rows retired as {RETYPED}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
