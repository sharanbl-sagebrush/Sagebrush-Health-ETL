# SBH_DATA_BANK — working notes

A 340B reconciliation platform for Sagebrush Health. **Scope today is the ETL
only**: getting nineteen vendor extracts into an append-only warehouse
correctly. Reconciliation is deliberately deferred until the ETL is signed off
against the definition of done in `docs/architecture-and-plan.md`.

## Read these first

| File | Why |
|---|---|
| `docs/architecture-and-plan.md` | The design, the phases, and the definition of done |
| `docs/data-findings.md` | 15 findings from the real data, each tied to the test that closes it or the question still open |
| `docs/file-audit.md` | Per-source structural facts from 67 file audits |

Commit messages explain *why*, not just what. `git log` is part of the record.

## The state of things

- 19 source specs in `specs/`, every one verified by loading the real extract
- Ledger schema live in `sbh_data_bank` on Azure (24 tables)
- 313 tests, no database required to run them
- **Not yet built**: the commit engine that writes a load into the ledger, and
  the historical backfill

## Rules that came from real defects

Each of these exists because the previous implementation got it wrong and it
cost something measurable. Changing one is a decision, not a cleanup.

**Never infer a date format.** `parse_date` requires an explicit format list.
Cardinal ships ISO in one pull and US in the next, and `01/02/2025` is
genuinely ambiguous. The previous code declared a format parameter and never
passed it.

**Money is `Decimal`, and `($40.00)` is negative forty.** Parenthesised
negatives are accounting notation. The previous parser nulled all 227,284 of
them across the payment files, overstating the total by $39,967,197.61.

**NDC normalization is not a left-pad.** `zfill(11)` implements the FDA 4-4-2
rule and only that rule; it was wrong on 54.5% of distinct NDCs, inventing
codes that do not exist. An unhyphenated 10-digit NDC cannot be disambiguated
without a directory lookup, so it quarantines rather than guesses.

**`required` means the column exists AND the row has a value.** An interior
blank row otherwise lands as a record with null everything.

**A spec with no natural key must justify itself in `notes`.** Five sources
genuinely have none — they are denormalised report extracts. That is a finding
about the data, and the model rejects a keyless spec that does not say so.

**Type columns by sampling values, never by name.** A name rule over Curve
Monthly Services matched three date columns and missed seven, including two
called `PIF_4` and `PIF_5`. Nothing would have failed; the dates would just
have been strings.

**Prohibited columns are matched and then discarded.** SSN appears in Monthly
Payments and Curve Monthly Services. The column is located so drift checks see
it, and no value reaches the raw layer, quarantine, or a log — including when
another column on the same row fails.

## Duplicates

Three policies, in `store/schema.py::DedupMode`, because nineteen sources need
three different answers:

- `natural_key` (14 sources) — same key, same hash → skip; same key, different
  hash → correction, insert new and supersede old
- `row_hash` (4) — no key exists; verified none contains byte-identical rows
- `file_position` (1) — Allscripts eRx only, where 8,100 of 132,974 rows are
  identical across all 21 columns

The natural-key index is **deliberately not unique**: superseded rows keep
their key, so versions coexist. Making it unique would break corrections.

Measured exposure: loading Cardinal's three overlapping deliveries naively
gives 1,914 duplicate rows (8.2%) and overstates purchases by $1,387,175.46.

## Layout

```
src/sbh_data_bank/
  core/       identity (deterministic hashing), temporal (effective dating)
  transforms/ ndc, values — the parsing vocabulary
  registry/   spec model, header matching, variant binding
  ingest/     streaming reader, row accounting, footer control totals
  store/      config, connection, ledger schema
specs/        19 source specs (YAML) — generated once, hand-edited since
tools/spec_build/  the generators that produced them, from real headers
scripts/      migrate, audit_files, bind_real_files, scaffold_spec
```

## Running things

```bash
python -m pytest                    # 313 tests, no database
python -m ruff check . && python -m ruff format .

python scripts/bind_real_files.py   # bind every spec against the real extracts
python scripts/audit_files.py       # structural audit of every file
python scripts/migrate.py --env-file <path>   # apply schema (idempotent)
```

The vendor extracts are PHI and live outside this repository. Point at them
with `SBH_SOURCE_ROOT`.

## Credentials

Never in this repository. `SqlSettings` reads `SBH_SQL_*` from the
environment, and `password` is excluded from `repr` so a settings object cannot
leak into a log line. Target posture is managed identity — omit
`SBH_SQL_USER` and the connection uses `ActiveDirectoryMsi`.

## Open questions for the business

These are in `docs/data-findings.md` with the evidence. They block
reconciliation, not ingestion.

1. **The `Class` → `Class_New` cutover date** in the Master COGS List. 28 of 121
   accounts were reclassified with no date recorded. Entity resolution reports
   `UNRESOLVED` for those until there is one.
2. **Which McKesson report is the purchase source.** Both parse to identical
   extended price, gross purchases and net quantity — loading both doubles every
   McKesson figure. Ordered quantity differs by 23 units.
3. **Whether `Monthly Payments` and `Curve_Monthly Services` overlap.** Both
   carry `Voucher_Number`, `Service_ID`, `IsVoided`, `ProcCode`, `Fee`.
4. **Four vendor asks** that would each remove a real problem: an order id from
   Allscripts, an appointment id from Curve, a line-sequence number on Curve
   Monthly Services, the encounter id on Athena Encounters.
