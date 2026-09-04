# recon340

A 340B reconciliation platform for covered entities. Configuration-driven ingestion,
inventory and financial reconciliation, built so that every number it produces can be
traced to the rows and rule versions that made it.

Working name. See `docs/` for the architecture and build plan.

## Status

Phase A, in progress. Foundation modules only; nothing is wired to a database yet.

| Module | State |
|---|---|
| `transforms.ndc` | Done - correct FDA 10-to-11 digit normalization with directory resolution |
| `core.temporal` | Done - effective dating, the "a rule is a row" primitive |
| everything else | Not started |

## Layout

```
src/recon340/
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
