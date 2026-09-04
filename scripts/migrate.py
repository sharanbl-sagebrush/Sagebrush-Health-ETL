"""Create or update the SBH_DATA_BANK schema from the source specs.

Forward-only and idempotent: every statement is guarded, so running it twice
is a no-op and running it after adding a column to a spec creates what is
missing. There is no down migration - undoing a schema change on an
append-only ledger means deciding what happens to the rows, which is a
conversation rather than a script.

    python scripts/migrate.py --env-file <path>   apply
    python scripts/migrate.py --dry-run           print the DDL, touch nothing

The env file holds a live credential and must live outside this repository.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.registry.loader import SpecRegistry  # noqa: E402
from sbh_data_bank.store.config import SqlSettings, load_env_file  # noqa: E402
from sbh_data_bank.store.connection import Database  # noqa: E402
from sbh_data_bank.store.schema import (  # noqa: E402
    dedup_mode_for,
    meta_statements,
    table_name,
    table_statements,
)

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--specs", type=Path, default=REPO / "specs")
    args = parser.parse_args()

    registry = SpecRegistry.from_dir(args.specs)
    statements: list[tuple[str, str]] = [("meta", s) for s in meta_statements()]
    for spec in registry:
        for statement in table_statements(spec):
            statements.append((spec.key, statement))

    print(f"{len(registry)} specs -> {len(statements)} statements")
    for spec in registry:
        print(f"  {table_name(spec):<48} dedup={dedup_mode_for(spec).value}")

    if args.dry_run:
        print("\n--- DDL ---")
        for _, statement in statements:
            print(statement.rstrip() + ";\n")
        return 0

    if args.env_file:
        load_env_file(args.env_file)
    settings = SqlSettings.from_env()
    print(f"\napplying to {settings.describe()}")

    applied = 0
    with Database(settings) as database:
        for owner, statement in statements:
            try:
                database.execute(statement)
                applied += 1
            except Exception as exc:  # noqa: BLE001 - report and stop, do not half-apply
                first = statement.strip().splitlines()[0][:90]
                print(f"\nFAILED [{owner}] {first}\n  {exc}")
                return 1

    print(f"applied {applied} statements")

    with Database(settings) as database:
        rows = database.execute(
            "SELECT s.name, t.name FROM sys.tables t "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id ORDER BY s.name, t.name"
        )
        print(f"\n{len(rows)} tables now present:")
        for schema, table in rows:
            print(f"  {schema}.{table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
