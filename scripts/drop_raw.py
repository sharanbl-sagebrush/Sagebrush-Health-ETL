"""Drop the ``raw`` schema's tables: the nineteen that predate the layered
design (CLAUDE.md, "The state of things") and were never written to.

    python scripts/drop_raw.py --env-file .env            # list, count, do nothing
    python scripts/drop_raw.py --env-file .env --apply    # drop them, then the schema

Refuses the whole operation if any table under ``raw`` holds a row: the
decision was to drop empty leftovers, not data. Decided 2026-11-06 by
the developer ("Drop the raw tables").
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.store.config import SqlSettings, load_env_file  # noqa: E402
from sbh_data_bank.store.connection import Database  # noqa: E402

TABLES = (
    "SELECT t.name, SUM(p.rows) FROM sys.tables t "
    "JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1) "
    "WHERE t.schema_id = SCHEMA_ID('raw') GROUP BY t.name ORDER BY t.name"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.env_file:
        load_env_file(args.env_file)
    settings = SqlSettings.from_env()
    with Database(settings) as db:
        cur = db.connection.cursor()
        cur.execute(TABLES)
        tables = [(name, int(rows)) for name, rows in cur.fetchall()]
        print(f"{len(tables)} tables under raw in {settings.describe()}")
        for name, rows in tables:
            print(f"  raw.{name:48} rows={rows}")
        if not tables:
            print("nothing to drop")
            return 0
        with_rows = [name for name, rows in tables if rows]
        if with_rows:
            print(f"REFUSED: {len(with_rows)} raw tables hold rows: {', '.join(with_rows)}")
            return 1
        if not args.apply:
            print("dry run: pass --apply to drop them")
            return 0
        for name, _rows in tables:
            cur.execute(f"DROP TABLE raw.[{name}]")
        cur.execute("SELECT COUNT(*) FROM sys.objects WHERE schema_id = SCHEMA_ID('raw')")
        if cur.fetchone()[0] == 0:
            cur.execute("DROP SCHEMA raw")
            print(f"dropped {len(tables)} tables and the raw schema")
        else:
            print(f"dropped {len(tables)} tables; the raw schema still holds other objects")
        db.connection.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
