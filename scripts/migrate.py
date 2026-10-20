"""Create or update the SBH_DATA_BANK schema from the source specs.

Forward-only and idempotent: every statement is guarded, so running it twice
is a no-op and running it after adding a column to a spec creates what is
missing. There is no down migration - undoing a schema change on an
append-only ledger means deciding what happens to the rows, which is a
conversation rather than a script.

    python scripts/migrate.py --env-file <path>   apply
    python scripts/migrate.py --dry-run           print the DDL, touch nothing

The env file holds a live credential and must live outside this repository.

How to run
    ``python scripts/migrate.py --dry-run``
        Print every statement that would run. Needs no database.
    ``python scripts/migrate.py --env-file C:\\secrets\\sbh.env``
        Load ``SBH_SQL_*`` from that file, connect, apply.
    ``python scripts/migrate.py``
        Same, but the ``SBH_SQL_*`` variables must already be in the
        environment (how production runs, populated from Key Vault).
    ``--specs <dir>`` points at a different spec folder; the default is
    ``specs/`` in this repository.

Inputs
    SBH_SQL_SERVER, SBH_SQL_DATABASE, SBH_SQL_DRIVER, and either
    SBH_SQL_USER + SBH_SQL_PASSWORD or neither (managed identity). See
    ``sbh_data_bank.store.config``. Values already in the environment win
    over the env file.

What it builds, in order
    1. ``meta_statements()`` from ``store/schema.py``: the ``meta`` and ``stg``
       schemas - ``meta.source_file`` (one row per physical file, keyed on
       content hash), ``meta.batch`` (one row per load attempt with the
       inserted / superseded / unchanged / quarantined counts),
       ``meta.run``, ``stg.quarantine`` and ``stg.duplicate_finding``.
    2. ``table_statements(spec)`` for every spec: three tables per source,
       ``landed.<source>`` (every column NVARCHAR(MAX), as delivered),
       ``typed.<source>`` (columns typed from the spec: ``NVARCHAR(1000)``,
       ``DECIMAL(38,4)`` for currency, ``CHAR(11)`` for NDC, and so on, plus
       the row hash) and ``curated.<source>`` (the ledger columns - batch,
       identity hash, occurrence, as-of, superseded_by - plus the typed
       columns again), with the live, identity and natural-key indexes.
       The ``raw.*`` tables of the first schema are left alone.

Why idempotent
    Every statement is wrapped in ``IF OBJECT_ID(...) IS NULL`` or
    ``IF NOT EXISTS``, so the script can be re-run after a spec gains a
    column and it creates only what is missing. It never drops or alters a
    column that exists; that is a decision about rows on an append-only
    ledger, not a migration.

What it does NOT do
    Load any data, verify the schema matches the specs after the fact, or
    migrate anything from the previous ``pharmacy_qa`` database (the plan
    is a fresh schema - docs/architecture-and-plan.md, "Decisions taken").

Outputs
    The list of raw tables with their dedup mode, the count of statements
    applied, and a listing of every table now present. Stops at the first
    failed statement with exit code 1 and the statement's first line, so a
    half-applied run is visible rather than silently partial.
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
    identity_columns_for,
    meta_statements,
    table_name,
    table_statements,
)

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    """Assemble the DDL from the specs, print it or apply it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--specs", type=Path, default=REPO / "specs")
    args = parser.parse_args()

    # Step 1: build the full statement list before connecting, so a spec
    # error is reported without a database round-trip. Each statement is
    # tagged with its owner ("meta" or the spec key) for the failure message.
    registry = SpecRegistry.from_dir(args.specs)
    statements: list[tuple[str, str]] = [("meta", s) for s in meta_statements()]
    for spec in registry:
        for statement in table_statements(spec):
            statements.append((spec.key, statement))

    print(f"{len(registry)} specs -> {len(statements)} statements")
    for spec in registry:
        identity = "key" if spec.natural_key else "all columns"
        print(
            f"  {table_name(spec, 'curated'):<48} identity={identity} "
            f"({len(identity_columns_for(spec))} cols)"
        )

    # Step 2 (dry run): print and stop. This is the way to review what a
    # spec change does to the schema before touching Azure.
    if args.dry_run:
        print("\n--- DDL ---")
        for _, statement in statements:
            print(statement.rstrip() + ";\n")
        return 0

    # Step 3: credentials. load_env_file uses setdefault, so a variable
    # already set in the process environment is never overridden by the
    # file. describe() prints server and database only, never the password.
    if args.env_file:
        load_env_file(args.env_file)
    settings = SqlSettings.from_env()
    print(f"\napplying to {settings.describe()}")

    # Step 4: apply one statement at a time. pyodbc runs one batch per call,
    # and executing separately means a failure names the exact statement.
    # Because every statement is guarded, stopping here and re-running later
    # picks up where this left off.
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

    # Step 5: read back the catalogue so the operator sees the actual state
    # of the database, not just what the script believes it did.
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
