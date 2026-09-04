# SBH_DATA_BANK

Sagebrush Health's pharmacy data bank. Configuration-driven ingestion of vendor
extracts into an append-only, entity-scoped warehouse, built so that every row can be
traced to the file, batch and rule version that produced it.

Reconciliation comes later and is deliberately out of scope until the ETL is signed off
against the definition of done in `docs/architecture-and-plan.md`.

## Status

Phase A, in progress. Foundation modules only; nothing is wired to a database yet.
Scope is the ETL through to canonical, entity-resolved events. Not reconciliation.

| Module | State |
|---|---|
| `transforms.ndc` | Done - correct FDA 10-to-11 digit normalization with directory resolution |
| `core.temporal` | Done - effective dating, the "a rule is a row" primitive |
| everything else | Not started |

## Layout

```
src/sbh_data_bank/
  core/         tenant context, effective dating, run identity, clock
  transforms/   the named transform vocabulary used by source specs
  registry/     source specs - how a vendor file becomes rows
  masterdata/   entities, sites, accounts, NDC dimension, ASP crosswalk
  store/        Azure SQL access, migrations, ledger primitives
  ingest/       acquire, parse, validate, quarantine, commit
  canonical/    vendor rows to canonical events
  expectations/ declarative data quality
tests/
  unit/         fast, no database
  fixtures/     golden data, identifiers only - no PHI
migrations/     forward-only SQL
```

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest          # unit tests, no database required
python -m ruff check .
python -m ruff format .
```

## PHI

Vendor extracts contain PHI. Nothing under `tests/fixtures/` may contain patient
identifiers - the fixtures committed here hold drug codes only. Real extracts stay
outside the repository and are excluded by `.gitignore`.
