"""Hold the curated layer against the staging layers, source by source:
nothing lost, nothing invented, no duplicate introduced, every value equal.

    python scripts/verify_layers.py --env-file .env
    python scripts/verify_layers.py --env-file .env --only <spec key> [--out <json>]

The staging layers are ``landed`` (every line as delivered text) and
``typed`` (the same lines parsed under the spec); the target is
``curated`` (the ledger the dedup rule writes). The three share the
(source_file_id, line_number) key, so every comparison is a set operation
inside the database and no row leaves it. The contract per source is
derived from the spec, not kept in a second list: the identity columns,
the business columns, and the one transformation between typed and
curated, which is the dedup rule (CLAUDE.md, "Duplicates").

What the rule does, and therefore what the checks allow:

* a typed line whose identity and hash a newer delivery already carries
  is *unchanged* and writes no curated row; the batch counts it
* every other typed line writes exactly one curated row per generation;
  a rebuild (D18) retires the previous generation as ``retyped`` and
  writes the line again, so a line may have two rows, one live-or-
  superseded and one retyped
* supersession (corrected, vanished, late) is the rule's output and is
  reported, never counted as loss

Checks, cheapest first, each reported per source:

1. **lines**: landed lines = typed rows + quarantined + skipped, where
   skipped (blank and footer lines) is what the batches recorded
2. **completeness**: a typed line with no current-generation curated row
   is unchanged (an earlier delivery already carried its identity and
   content), and its content must then be on some current-generation
   row; lines whose content is nowhere are loss. Without a rebuild the
   count equals what the loads recorded; after one, the new rule may
   count a line unchanged that the load did not, so the recorded count
   is reported beside it. Curated rows with no typed line are 0
3. **duplicates**: 0 identities with two current rows; 0 lines with two
   current-generation rows; the vendor's own repeats are reported as
   occurrences and findings, not failures
4. **fingerprints and values**: for every current-generation row, the
   curated row hash equals the typed one and every business column is
   equal, null for null
5. **aggregates**: for every numeric column, the typed total equals the
   current-generation curated total plus the total over the unchanged
   lines; the current total is reported beside it for the reader

The verdict is the last line; exit code 1 when any check fails. The
results go to a JSON file (``--out``) with counts, column names and
(file id, line number) samples of any failure.

PHI: prints spec keys, file names, counts, column names and line numbers.
Never a cell value.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.registry.loader import SpecRegistry  # noqa: E402
from sbh_data_bank.registry.spec import FieldType, SourceSpec  # noqa: E402
from sbh_data_bank.store.config import SqlSettings, load_env_file  # noqa: E402
from sbh_data_bank.store.connection import Database  # noqa: E402
from sbh_data_bank.store.schema import _all_targets, identity_columns_for, table_name  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
NUMERIC = {FieldType.INTEGER, FieldType.DECIMAL, FieldType.CURRENCY}
SAMPLE = 5
#: A row of the current generation: live, or superseded by the rule, but
#: not retired by a rebuild (that generation was typed under an older spec).
CURRENT_GEN = "(c.supersede_reason IS NULL OR c.supersede_reason <> 'retyped')"


def _one(cur, sql: str, *params) -> tuple:
    cur.execute(sql, *params)
    return tuple(cur.fetchone())


def _rows(cur, sql: str, *params) -> list[tuple]:
    cur.execute(sql, *params)
    return [tuple(r) for r in cur.fetchall()]


def _eq(col: str) -> str:
    """Null-safe inequality of one business column between c and t."""
    same = f"c.[{col}] = t.[{col}] OR (c.[{col}] IS NULL AND t.[{col}] IS NULL)"
    return f"SUM(CASE WHEN {same} THEN 0 ELSE 1 END)"


def verify_source(cur, spec: SourceSpec, tenant: str) -> dict[str, Any]:
    landed, typed, curated = (table_name(spec, layer) for layer in ("landed", "typed", "curated"))
    targets = _all_targets(spec)
    columns = [t for t, _k, _m in targets]
    numeric = [t for t, k, _m in targets if k in NUMERIC]
    result: dict[str, Any] = {
        "source": spec.key,
        "identity": list(identity_columns_for(spec)) if spec.natural_key else "every column",
        "columns": len(columns),
        "checks": {},
        "failures": [],
    }
    checks = result["checks"]

    def fail(check: str, detail: str, sample: list | None = None) -> None:
        result["failures"].append({"check": check, "detail": detail, "sample": sample or []})

    # --- the batches: what the loads recorded -----------------------------
    batches = _rows(
        cur,
        "SELECT b.batch_id, b.outcome, b.rows_landed, b.rows_read, b.rows_unchanged, "
        "b.rows_quarantined, b.rows_inserted, b.rows_superseded, b.rows_vanished "
        "FROM meta.batch b WHERE b.tenant_id = ? AND b.source_key = ? "
        "AND b.outcome IN ('LOADED', 'REBUILT')",
        tenant,
        spec.key,
    )
    loaded = [b for b in batches if b[1] == "LOADED"]
    rebuilt = [b for b in batches if b[1] == "REBUILT"]
    recorded = {
        "files_loaded": len(loaded),
        "rebuilds": len(rebuilt),
        "rows_landed": sum(b[2] for b in loaded),
        "rows_read": sum(b[3] for b in loaded),
        "rows_unchanged": sum(b[4] for b in loaded),
        "rows_quarantined": sum(b[5] for b in loaded),
        "rows_inserted": sum(b[6] for b in loaded),
        "rows_superseded": sum(b[7] for b in loaded),
        "rows_vanished": sum(b[8] for b in loaded),
    }
    checks["recorded"] = recorded
    if not loaded:
        result["status"] = "NEVER_LOADED"
        return result

    # --- 1. lines: landed = typed + quarantined + skipped ------------------
    (n_landed,) = _one(cur, f"SELECT COUNT(*) FROM {landed} WHERE tenant_id = ?", tenant)
    (n_typed,) = _one(cur, f"SELECT COUNT(*) FROM {typed} WHERE tenant_id = ?", tenant)
    (n_quar,) = _one(
        cur,
        "SELECT COUNT(*) FROM stg.quarantine WHERE tenant_id = ? AND source_key = ?",
        tenant,
        spec.key,
    )
    skipped_recorded = recorded["rows_landed"] - recorded["rows_read"]
    skipped_found = n_landed - n_typed - n_quar
    checks["lines"] = {
        "landed": n_landed,
        "typed": n_typed,
        "quarantined": n_quar,
        "skipped": skipped_found,
        "skipped_recorded": skipped_recorded,
        "landed_recorded": recorded["rows_landed"],
    }
    if n_landed != recorded["rows_landed"]:
        fail(
            "lines",
            f"landed holds {n_landed} lines, the batches recorded {recorded['rows_landed']}",
        )
    if skipped_found != skipped_recorded:
        fail(
            "lines",
            f"{skipped_found} landed lines are neither typed nor quarantined; "
            f"the batches recorded {skipped_recorded} skipped",
        )
    if n_quar:
        fail("lines", f"{n_quar} quarantined rows (no quarantine was expected on any source)")

    # --- 2. completeness between typed and curated ------------------------
    # A typed line with no current-generation curated row is "unchanged":
    # its identity and content were already current from an earlier
    # delivery (the load counted it on the batch and wrote nothing, or a
    # rebuild retired its old row and found the new rule produced none).
    # The proof that nothing was lost is that its content exists on a
    # current-generation row: same row hash, somewhere in curated.
    no_current_gen = (
        f"NOT EXISTS (SELECT 1 FROM {curated} c WHERE c.source_file_id = t.source_file_id "
        f"AND c.line_number = t.line_number AND {CURRENT_GEN})"
    )
    # The other way a line has no current-generation row: a rebuild (D18)
    # writes the state the new rule makes current and retires the live rows
    # it no longer produces as "retyped"; what the new rule would have made
    # history inside the replay (a line a newer delivery corrected, a late
    # line) is not written, by design (commit/rebuild.py). Such a line is
    # accounted for when a current-generation row of the same identity
    # carries a newer or equal as-of. Only a keyed source can show that; a
    # keyless identity is the whole row, which the hash already covers.
    identity = identity_columns_for(spec) if spec.natural_key else ()
    same_identity = " AND ".join(
        f"(c.[{k}] = t.[{k}] OR (c.[{k}] IS NULL AND t.[{k}] IS NULL))" for k in identity
    )
    typed_as_of = (
        "(SELECT f.as_of FROM meta.source_file f WHERE f.source_file_id = t.source_file_id)"
    )
    superseded_by_newer = (
        f"EXISTS (SELECT 1 FROM {curated} c WHERE c.tenant_id = t.tenant_id AND {CURRENT_GEN} "
        f"AND c.as_of >= {typed_as_of} AND NOT (c.source_file_id = t.source_file_id "
        f"AND c.line_number = t.line_number) AND {same_identity})"
        if identity
        else "1 = 0"
    )
    # SQL Server refuses a subquery inside an aggregate, so the flags are
    # computed per typed line in a derived table and summed outside it.
    (missing, unchanged, made_history, lost) = _one(
        cur,
        f"SELECT SUM(x.missing), SUM(x.unchanged), SUM(x.made_history), SUM(x.lost) FROM ("
        f"SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM {curated} c "
        f"WHERE c.source_file_id = t.source_file_id AND c.line_number = t.line_number) "
        f"THEN 1 ELSE 0 END AS missing, "
        f"CASE WHEN {no_current_gen} THEN 1 ELSE 0 END AS unchanged, "
        f"CASE WHEN {no_current_gen} AND NOT EXISTS (SELECT 1 FROM {curated} c "
        f"WHERE c.tenant_id = t.tenant_id AND c.row_hash = t.row_hash AND {CURRENT_GEN}) "
        f"AND {superseded_by_newer} THEN 1 ELSE 0 END AS made_history, "
        f"CASE WHEN {no_current_gen} AND NOT EXISTS (SELECT 1 FROM {curated} c "
        f"WHERE c.tenant_id = t.tenant_id AND c.row_hash = t.row_hash AND {CURRENT_GEN}) "
        f"AND NOT {superseded_by_newer} THEN 1 ELSE 0 END AS lost "
        f"FROM {typed} t WHERE t.tenant_id = ?) AS x",
        tenant,
    )
    missing, unchanged, made_history, lost = (
        int(missing or 0),
        int(unchanged or 0),
        int(made_history or 0),
        int(lost or 0),
    )
    (orphans,) = _one(
        cur,
        f"SELECT COUNT(*) FROM {curated} c WHERE c.tenant_id = ? AND NOT EXISTS "
        f"(SELECT 1 FROM {typed} t WHERE t.source_file_id = c.source_file_id "
        f"AND t.line_number = c.line_number)",
        tenant,
    )
    (n_curated, n_current, n_retyped, n_corrected, n_vanished, n_late) = _one(
        cur,
        f"SELECT COUNT(*), SUM(CASE WHEN superseded_by IS NULL THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN supersede_reason = 'retyped' THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN supersede_reason = 'corrected' THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN supersede_reason = 'vanished' THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN supersede_reason = 'late' THEN 1 ELSE 0 END) "
        f"FROM {curated} WHERE tenant_id = ?",
        tenant,
    )
    checks["completeness"] = {
        "typed_lines_without_curated_row": missing,
        "typed_lines_unchanged": unchanged,
        "unchanged_recorded_by_loads": recorded["rows_unchanged"],
        "unchanged_lines_a_rebuild_made_history_without_writing": made_history,
        "unchanged_lines_whose_content_is_on_no_current_generation_row": lost,
        "curated_rows_without_typed_line": orphans,
        "curated_rows": n_curated,
        "current": n_current,
        "superseded_corrected": n_corrected,
        "superseded_vanished": n_vanished,
        "superseded_late": n_late,
        "retired_retyped": n_retyped,
    }
    if lost:
        sample = _rows(
            cur,
            f"SELECT TOP {SAMPLE} t.source_file_id, t.line_number FROM {typed} t "
            f"WHERE t.tenant_id = ? AND {no_current_gen} AND NOT EXISTS (SELECT 1 FROM {curated} c "
            f"WHERE c.tenant_id = t.tenant_id AND c.row_hash = t.row_hash AND {CURRENT_GEN}) "
            f"AND NOT {superseded_by_newer} "
            f"ORDER BY t.source_file_id, t.line_number",
            tenant,
        )
        fail(
            "completeness",
            f"{lost} typed lines have no current-generation curated row and their content "
            f"is on no other row either",
            sample,
        )
    if not rebuilt and missing != recorded["rows_unchanged"]:
        # Without a rebuild the loads' own count is exact; after one, the
        # new rule may count a line unchanged that the load did not.
        fail(
            "completeness",
            f"{missing} typed lines have no curated row; the loads recorded "
            f"{recorded['rows_unchanged']} unchanged",
        )
    if orphans:
        sample = _rows(
            cur,
            f"SELECT TOP {SAMPLE} c.source_file_id, c.line_number FROM {curated} c "
            f"WHERE c.tenant_id = ? AND NOT EXISTS (SELECT 1 FROM {typed} t "
            f"WHERE t.source_file_id = c.source_file_id AND t.line_number = c.line_number)",
            tenant,
        )
        fail("completeness", f"{orphans} curated rows have no typed line", sample)

    # --- 3. duplicates ----------------------------------------------------
    (double_current,) = _one(
        cur,
        f"SELECT COUNT(*) FROM (SELECT identity_hash, occurrence FROM {curated} "
        f"WHERE tenant_id = ? AND superseded_by IS NULL "
        f"GROUP BY identity_hash, occurrence HAVING COUNT(*) > 1) d",
        tenant,
    )
    (double_gen,) = _one(
        cur,
        f"SELECT COUNT(*) FROM (SELECT source_file_id, line_number FROM {curated} c "
        f"WHERE c.tenant_id = ? AND {CURRENT_GEN} "
        f"GROUP BY source_file_id, line_number HAVING COUNT(*) > 1) d",
        tenant,
    )
    (occurrences, identical_current) = _one(
        cur,
        f"SELECT SUM(CASE WHEN occurrence > 1 THEN 1 ELSE 0 END), "
        f"(SELECT COUNT(*) FROM (SELECT row_hash FROM {curated} WHERE tenant_id = ? "
        f"AND superseded_by IS NULL GROUP BY row_hash HAVING COUNT(*) > 1) x) "
        f"FROM {curated} c WHERE c.tenant_id = ? AND {CURRENT_GEN}",
        tenant,
        tenant,
    )
    (findings,) = _one(
        cur,
        "SELECT COUNT(*) FROM stg.duplicate_finding WHERE tenant_id = ? AND source_key = ?",
        tenant,
        spec.key,
    )
    checks["duplicates"] = {
        "identities_with_two_current_rows": double_current,
        "lines_with_two_current_generation_rows": double_gen,
        "vendor_repeats_as_occurrences": occurrences or 0,
        "vendor_repeats_recorded_as_findings": findings,
        "current_rows_sharing_a_row_hash": identical_current,
    }
    if double_current:
        fail("duplicates", f"{double_current} identities have two current rows")
    if double_gen:
        fail("duplicates", f"{double_gen} lines have two current-generation curated rows")

    # --- 4. fingerprints and values, current generation -------------------
    exprs = ["COUNT(*)", "SUM(CASE WHEN c.row_hash <> t.row_hash THEN 1 ELSE 0 END)"]
    exprs += [_eq(col) for col in columns]
    row = _one(
        cur,
        f"SELECT {', '.join(exprs)} FROM {curated} c JOIN {typed} t "
        f"ON t.source_file_id = c.source_file_id AND t.line_number = c.line_number "
        f"WHERE c.tenant_id = ? AND {CURRENT_GEN}",
        tenant,
    )
    compared, hash_mismatch = row[0], row[1] or 0
    differing = {col: int(n) for col, n in zip(columns, row[2:], strict=True) if n}
    checks["values"] = {
        "rows_compared": compared,
        "row_hash_mismatches": hash_mismatch,
        "columns_compared": len(columns),
        "columns_with_a_difference": differing,
    }
    if hash_mismatch:
        sample = _rows(
            cur,
            f"SELECT TOP {SAMPLE} c.source_file_id, c.line_number FROM {curated} c "
            f"JOIN {typed} t ON t.source_file_id = c.source_file_id "
            f"AND t.line_number = c.line_number WHERE c.tenant_id = ? AND {CURRENT_GEN} "
            f"AND c.row_hash <> t.row_hash",
            tenant,
        )
        fail(
            "values",
            f"{hash_mismatch} current-generation rows hash differently from typed",
            sample,
        )
    for col, n in differing.items():
        sample = _rows(
            cur,
            f"SELECT TOP {SAMPLE} c.source_file_id, c.line_number FROM {curated} c "
            f"JOIN {typed} t ON t.source_file_id = c.source_file_id "
            f"AND t.line_number = c.line_number WHERE c.tenant_id = ? AND {CURRENT_GEN} "
            f"AND NOT (c.[{col}] = t.[{col}] OR (c.[{col}] IS NULL AND t.[{col}] IS NULL))",
            tenant,
        )
        fail("values", f"column {col} differs on {n} rows", sample)

    # --- 5. aggregates over the numeric columns ---------------------------
    aggregates: dict[str, dict[str, str]] = {}
    if numeric:
        sums_typed = _one(
            cur,
            "SELECT "
            + ", ".join(f"SUM(t.[{c}])" for c in numeric)
            + f" FROM {typed} t WHERE t.tenant_id = ?",
            tenant,
        )
        sums_unchanged = _one(
            cur,
            "SELECT "
            + ", ".join(f"SUM(t.[{c}])" for c in numeric)
            + f" FROM {typed} t WHERE t.tenant_id = ? AND {no_current_gen}",
            tenant,
        )
        sums_gen = _one(
            cur,
            "SELECT "
            + ", ".join(f"SUM(c.[{c}])" for c in numeric)
            + f" FROM {curated} c WHERE c.tenant_id = ? AND {CURRENT_GEN}",
            tenant,
        )
        sums_current = _one(
            cur,
            "SELECT "
            + ", ".join(f"SUM(c.[{c}])" for c in numeric)
            + f" FROM {curated} c WHERE c.tenant_id = ? AND c.superseded_by IS NULL",
            tenant,
        )
        for i, col in enumerate(numeric):
            typed_sum = sums_typed[i] or 0
            explained = (sums_gen[i] or 0) + (sums_unchanged[i] or 0)
            aggregates[col] = {
                "typed": str(typed_sum),
                "current_generation_plus_unchanged": str(explained),
                "current": str(sums_current[i] or 0),
            }
            if typed_sum != explained:
                fail(
                    "aggregates",
                    f"column {col}: typed total {typed_sum} but current generation plus "
                    f"unchanged lines total {explained}",
                )
    checks["aggregates"] = aggregates

    result["status"] = "FAIL" if result["failures"] else "PASS"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--specs", type=Path, default=REPO / "specs")
    parser.add_argument("--out", type=Path, help="write the results as JSON here")
    args = parser.parse_args()
    registry = SpecRegistry.from_dir(args.specs)
    if args.env_file:
        load_env_file(args.env_file)
    settings = SqlSettings.from_env()
    only = set(args.only)
    started = datetime.now(UTC)
    print(f"verifying the layers of {settings.describe()}")
    results = []
    with Database(settings) as db:
        db.connection.timeout = 3600
        cur = db.connection.cursor()
        for spec in registry:
            if only and spec.key not in only:
                continue
            t0 = time.time()
            result = verify_source(cur, spec, settings.tenant)
            result["seconds"] = round(time.time() - t0, 1)
            results.append(result)
            c = result["checks"]
            if result["status"] == "NEVER_LOADED":
                print(f"  {spec.key:40} NEVER_LOADED")
                continue
            print(
                f"  {spec.key:40} {result['status']:4} landed={c['lines']['landed']} "
                f"typed={c['lines']['typed']} current={c['completeness']['current']} "
                f"compared={c['values']['rows_compared']} "
                f"double_current={c['duplicates']['identities_with_two_current_rows']} "
                f"({result['seconds']}s)"
            )
            for f in result["failures"]:
                sample = f" sample={f['sample']}" if f["sample"] else ""
                print(f"      {f['check']}: {f['detail']}{sample}")
    failed = [r for r in results if r["status"] == "FAIL"]
    report = {
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "database": settings.describe(),
        "sources": len(results),
        "passed": sum(1 for r in results if r["status"] == "PASS"),
        "failed": len(failed),
        "never_loaded": sum(1 for r in results if r["status"] == "NEVER_LOADED"),
        "results": results,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"results written to {args.out}")
    print(
        f"\nverdict: {report['passed']} sources pass, {len(failed)} fail, "
        f"{report['never_loaded']} never loaded"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
