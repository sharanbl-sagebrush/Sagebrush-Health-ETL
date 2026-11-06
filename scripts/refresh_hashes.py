"""Bring a source's curated row hashes back in step with its typed rows.

    python scripts/refresh_hashes.py --env-file .env --only <spec key>          # count
    python scripts/refresh_hashes.py --env-file .env --only <spec key> --apply  # write

A curated row's ``row_hash`` is the fingerprint of its typed line, so the
two must agree on every (file, line). A rebuild with a type change used
to refill the retyped values into the history rows without the hash
(found 2026-11-06 by ``scripts/verify_layers.py``: 109,190 Curve services
rows); the rebuild now carries the hash with the values, and this script
is the one-time repair for rows written before that. It changes no
business value and no live row's standing: only the fingerprint column,
and only where it differs from typed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.commit.sql import refresh_hashes_sql  # noqa: E402
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
            cur.execute(refresh_hashes_sql(spec, count_only=True), settings.tenant)
            (stale,) = cur.fetchone()
            cur.execute(
                f"SELECT COUNT(*) FROM {curated} AS c JOIN {typed} AS ty "
                f"ON ty.source_file_id = c.source_file_id AND ty.line_number = c.line_number "
                f"WHERE c.tenant_id = ? AND c.superseded_by IS NULL AND c.row_hash <> ty.row_hash",
                settings.tenant,
            )
            (live,) = cur.fetchone()
            print(
                f"{key}: {stale} history rows hash differently from their typed line; "
                f"{live} live rows do (must be 0)"
            )
            if live:
                print(f"{key}: REFUSED, a live row out of step is a rebuild's job, not this one's")
                return 1
            if not stale or not args.apply:
                continue
            cur.execute(refresh_hashes_sql(spec, count_only=False), settings.tenant)
            print(f"{key}: {cur.rowcount} rows refreshed")
            db.connection.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
