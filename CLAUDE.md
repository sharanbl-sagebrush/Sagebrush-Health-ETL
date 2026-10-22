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
| `docs/decision-record.md` | Every decision the engine depends on since 2026-10-16, one page, with who took it |
| `docs/layer-walk/README.md` | The 19-source replay of the dedup rule: scorecard, decisions D1-D10, load order |

Commit messages explain *why*, not just what. `git log` is part of the record.

## The state of things

- 19 source specs in `specs/`, every one verified by loading the real extract
- `store/schema.py` generates the layered DDL from the specs: `landed`,
  `typed` and `curated` tables per source plus `meta` and `stg`
  (`python scripts/migrate.py --dry-run` prints it, 153 statements).
  **Applied to `sbh_data_bank` on Azure 2026-10-21**: 81 tables, the 57
  layered ones plus `meta` and `stg`. The 19 `raw.*` tables predate the
  layered decision, were never written to, and are left alone until dropped
  explicitly. Re-running the migration is a no-op
- 460 tests, no database required to run them
- Decided 2026-10-16: the two `voidsign` keys are applied; design defaults
  D1-D11 are recorded in `docs/decision-record.md` and reviewed one source
  at a time in the layer-walk load order, McKesson Purchasing Detail first
- Done 2026-10-20: D1-D6 and D11 are implemented (spec fields, `curate/`,
  audit columns); both McKesson specs declare their event date, coverage and
  vanish scope and their walks are regenerated with the `vanished` outcome
  running. D7 (`datetime` type) waits for the first source that needs it
- Done 2026-10-20: the commit engine (`commit/`) writes a load through the
  layers in one transaction and persists what `curate.rule` decides; replayed
  in memory over the real McKesson files it reproduces the walk exactly
  (7,124 and 6,415 current rows, 11 corrections each)
- Done 2026-10-21: **first real load**. The five McKesson Purchasing Detail
  files committed to `sbh_data_bank` in as-of order: four LOADED, the
  byte-identical copy recorded as `DUPLICATE_FILE`. The database agrees with
  the replay exactly: 7,124 current rows, 11 corrected, extended price
  23,305,296.76, 15 duplicate findings, 0 quarantined, no identity with two
  current rows
- Done 2026-10-22: **the backfill**. `scripts/backfill.py` committed every
  delivery of fourteen sources in as-of order on the batched insert path
  (`--fast`, every parameter declared from the DDL, proved with 276
  read-back checks and 0 mismatches). Zero quarantined, zero conflicts,
  engine counters identical to the dry runs. Results and the two decisions
  it forced (D14 creation stamps outside the hash, D15 declared scale) are
  in `docs/decision-record.md`
- **Not yet built**: the acquire step, the quarantine-rate threshold,
  alerting, re-running typed from landed in the database

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

**A vendor column may not be named like a ledger column.** Every generated
table carries `created_at`, `batch_id`, `as_of` and the other names in
`RESERVED_COLUMN_NAMES`. WeInfuse's `Created At` was mapped to `created_at`
and the first Azure migration failed on it (2026-10-21). The spec model
rejects the name; the convention is `vendor_<name>`.

**A spec with no natural key must justify itself in `notes`.** Five sources
genuinely have none — they are denormalised report extracts. That is a finding
about the data, and the model rejects a keyless spec that does not say so.

**Type columns by sampling values, never by name.** A name rule over Curve
Monthly Services matched three date columns and missed seven, including two
called `PIF_4` and `PIF_5`. Nothing would have failed; the dates would just
have been strings.

**Prohibited columns are matched and then discarded.** SSN appears in Monthly
Payments and Curve Monthly Services. The column is located so drift checks see
it, and no value reaches any layer, quarantine, or a log — including when
another column on the same row fails.

**A number is rounded to its column's declared scale before it is hashed.**
WellPartner's estimated wholesale cost carries seven decimal places; the
ledger holds money at four. The batched insert refused it (2026-10-21), where
the row-at-a-time path would have let SQL Server round silently behind a hash
of the unrounded value. The scales live once in `transforms.values`, the DDL
is built from them, and a value that already fits is never touched (D15).

**A vendor's creation stamp is stored but not hashed.** WeInfuse re-formats
`Created At` on every export; without `row_hash_exclude` the second delivery
corrected 19,643 of 19,644 unchanged rows (D14).

## Storage layers

Decided 2026-10-07; reasoning in `docs/architecture-and-plan.md`, "Storage
layers". Medallion pattern, four layers:

| Layer | Holds | Rebuilt from |
|---|---|---|
| `landed` | every cell as delivered text, one row per file line | files, once, never changed |
| `typed` | parsed values, quarantine, row hash | landed |
| `curated` | dedup, supersession, site resolution, canonical events, rule-versioned | typed |
| `pres` | views and marts for users | curated |

Nothing is dropped before landed except prohibited columns (SSN), which are
matched for drift and discarded at the boundary. Every step between layers is
a versioned rule re-runnable from the layer below, so a wrong key or format is
a re-run in the database, never a re-read of files. One spec still drives all
three steps.

## Duplicates

Decided 2026-10-15: **one rule for all nineteen sources**, applied
typed-to-curated, versioned as `store/schema.py::DEDUP_RULE`. Reasoning and
evidence in `docs/architecture-and-plan.md`, "Duplicates and delivery order".

**Identity** of a row = its identity columns + an occurrence number.

- Identity columns are the spec's `natural_key`; a keyless spec uses every
  landed column (`identity_columns_for(spec)`).
- Occurrence number: rows in one delivery that share identity columns are
  numbered 1, 2, 3 in file order. So the two McKesson split-shipment lines
  are (key, 1) and (key, 2) and both survive; the 8,100 byte-identical
  Allscripts rows are 8,100 identities. This replaces the old three modes.

**As-of** of a delivery = when the vendor extracted it, in this order
(D5, `curate/delivery.py`): the dated upload folder the file sits in → the
spec's `as_of_source` override, for backlog files → xlsx internal creation
stamp (`docProps/core.xml`, written by the vendor portal and verified
against real files) → the end date in the filename → a date the operator
supplies. None of those: the file is refused. The as-of may not be earlier
than the latest event date in the file, with one day's tolerance (D4).
Upload time is recorded separately and never used for ordering.

**Coverage and vanish** (D1-D3): a spec declares its `event_date`, where
its coverage window comes from and the `vanish_scope` columns a delivery
speaks for. A current row is superseded as `vanished` only when a newer
delivery covers its date and its scope values and no longer carries its
identity. Fields are in `specs/README.md`; the rule is `curate/rule.py`.
Vanish stays on for McKesson and is applied, not merely reported (decided
2026-10-20). D12: for a `coverage: filename` source the engine holds a
file whose rows contradict the window its name claims.

**What the commit engine refuses or holds** (`commit/engine.py`): a
duplicate content hash, a load that is not ok, no as-of, an as-of floor
violation, an unknowable coverage window on a source that vanishes, a
filename window the data contradicts, and a conflict. Every one leaves a
`meta.source_file` and a `meta.batch` row and nothing else. A late file is
not held: it lands in full and enters `curated` already superseded.

**Outcome** per incoming row, against the current row with the same identity:

| Incoming vs current | Outcome |
|---|---|
| no current row | insert |
| newer as-of, same hash | unchanged |
| newer as-of, different hash | correction: insert, supersede current |
| older as-of | late: stored as history only, never current |
| same as-of, different hash | conflict: batch held for a person |
| current row absent from a newer delivery that covers its date | vanished: supersede |

Dedup never decides which *business* line is right. A McKesson return is a
separate line with its own credit number (31 of 31 in the history file) and
is inserted beside the original; reconciliation sums the two.

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
  curate/     delivery facts (as-of, coverage, as-of floor) and the dedup
              rule with vanish, in memory
  commit/     the commit engine: one file per transaction through meta,
              landed, typed, curated and quarantine; a SQL store and an
              in-memory store behind one interface
  store/      config, connection, ledger schema
specs/        19 source specs (YAML) — generated once, hand-edited since
tools/spec_build/  the generators that produced them, from real headers
scripts/      migrate, audit_files, bind_real_files, scaffold_spec, layer_walk,
              trace_file, commit_file, backfill
```

## Running things

```bash
python -m pytest                    # 460 tests, no database
python -m ruff check . && python -m ruff format .

python scripts/bind_real_files.py   # bind every spec against the real extracts
python scripts/audit_files.py       # structural audit of every file
python scripts/migrate.py --env-file <path>   # apply schema (idempotent)
python scripts/commit_file.py <spec key> "<file>" --dry-run   # the engine, in memory
python scripts/commit_file.py <spec key> "<file>" --env-file <path>   # commit to Azure
python scripts/backfill.py --dry-run --only <spec key>        # every delivery, in memory
python scripts/backfill.py --env-file .env --actor <you> --fast [--only ...]   # to Azure
```

Always pass `--fast` for a real load: the batched path is the proven one and
the row-at-a-time path takes minutes per thousand rows. A dry run of every
source at once holds every row in memory; run `--only` one source at a time.

The vendor extracts are PHI and live outside this repository. Point at them
with `SBH_SOURCE_ROOT`.

## How files arrive

Decided 2026-10-07. Files are downloaded by hand from each vendor portal and
uploaded to Azure Storage every Sunday, with ad hoc midweek re-uploads when
data needs correcting. The ETL must pick up a midweek file without waiting for
Sunday. So:

- The acquire step (not yet built) polls the blob container or reacts to
  blob-created events. It is never a Sunday cron.
- `expected_cadence_days` is a staleness threshold ("alert if nothing seen in
  N days"), not a schedule. Specs keep 7 for weekly sources; the alerting
  engine adds grace so a Sunday-morning check does not fire before the upload.
- Overlapping deliveries are normal. Which delivery wins is decided by each
  delivery's **as-of date** (when the vendor extracted it), never by when it
  was uploaded. See "Duplicates" below for the rule and where as-of comes
  from. Full reasoning in `docs/architecture-and-plan.md`, "Duplicates and
  delivery order".

## Credentials

Never committed. `SqlSettings` reads `SBH_SQL_*` from the environment, and
`password` is excluded from `repr` so a settings object cannot leak into a
log line. For development the five settings live in `.env` in the repository
root, which `.gitignore` excludes (`*.env`, decided 2026-10-21); scripts take
`--env-file .env`. Target posture is managed identity — omit `SBH_SQL_USER`
and the connection uses `ActiveDirectoryMsi`.

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
