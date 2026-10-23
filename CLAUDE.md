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

- 21 source specs in `specs/`, every one verified by loading the real extract
  (Cardinal's summary and detail reports are two sources since 2026-10-22, D16;
  the Apexus pricing workbook since 2026-10-23)
- `store/schema.py` generates the layered DDL from the specs: `landed`,
  `typed` and `curated` tables per source plus `meta` and `stg`
  (`python scripts/migrate.py --dry-run` prints it, 172 statements).
  **Applied to `sbh_data_bank` on Azure 2026-10-21**, re-applied 2026-10-22
  after the fifteen empty tables of five changed specs were dropped by
  hand, and again 2026-10-23 for the pricing source, `meta.blob` and
  `meta.spec_check`: 63 layered tables plus `meta` and `stg`. The 19 `raw.*` tables
  predate the layered decision, were never written to, and are left alone
  until dropped explicitly. Re-running the migration is a no-op; it only
  creates what is missing and never alters a column, so a spec change to a
  table with rows is a decision about those rows first
- 512 tests, no database required to run them
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
- Done 2026-10-22, later: **every source is in Azure**. The five changed
  specs had their empty tables recreated and loaded, the Cardinal summary
  loaded as its own source (D16), and the two new upload folders (May to
  September 2026) went in with them, for all eighteen sources that had
  them: zero refused, zero quarantined, zero conflicts, the six changed
  sources read back and balanced. Three decisions came out of the new
  files: D16, D17 (a file past its quarantine rate is held) and the text
  width rule below. Numbers in `docs/decision-record.md`, "The second load"
- Done 2026-10-22, evening: **the freshness check and the landed-layer
  replay**. `scripts/freshness.py` reads the metadata tables and reports
  STALE, NEVER_LOADED, OPEN_REFUSAL, RUN_FAILED and RUN_UNFINISHED as
  alerts (exit code 1) and open quarantine as a note; its first run found
  one real alert, Monthly Payments with nothing since 2026-07-21.
  `scripts/retype.py` types every loaded file again from `landed` plus the
  recorded header, without the file, and compares line by line with
  `typed` and `stg.quarantine`: over the whole database every line came
  back with the same hash and every control total agreed, which is the
  proof that the landed layer is complete. It reports only; writing the
  rebuilt rows is D18, not yet decided. The two run rows the first
  backfill left open are closed as INTERRUPTED (`scripts/close_runs.py`)
- Done 2026-10-23: **the pricing workbook is a source**
  (`reference.apexus.pricing`, the `apexus` sheet, 45,551 rows loaded, as-of
  from the workbook's modified stamp until it arrives in a dated folder);
  **the acquire step is built** (`acquire/`, `scripts/acquire.py`: mirror
  the upload container into a landing root, record each blob in
  `meta.blob`, run the backfill loop over it) and waits only for a data
  role on the container; **email alerts are built** (`observe/notify.py`,
  `--email` on the freshness check, the acquire pass mails its report)
  and wait only for the SMTP settings and recipients; the freshness check
  gained `SPEC_CHANGED` (a spec edited after its last load, cleared by a
  `retype.py` verdict recorded in `meta.spec_check`) and `NEW_COLUMNS`
  (vendor columns no spec column claims, as a note). The payments versus
  services question is answered (`docs/data-findings.md`, 17)
- 2026-10-23, later: **hosting built and paused**. `Dockerfile` and
  `deploy/deploy.sh` make two scheduled Container Apps jobs (acquire every
  half hour, freshness daily); the registry and environment exist in
  `sbh-data-bank-rg`, the jobs do not. The developer wants the loaded data
  verified against the raw files first (`scripts/verify_load.py`); nothing
  in Azure changes until they say so. The reconciliation question (can
  Curve Monthly Services be reconciled with WeInfuse dispenses) is
  answered with numbers in `docs/data-findings.md`, 18: yes, on patient
  number, day and HCPCS, with a code crosswalk as the first rule
- 2026-10-23, verified: **the database matches the raw files**
  (`scripts/verify_load.py`, `docs/data-findings.md` 19): 208 files
  loaded, every line count matched, 0 missing, 0 double-current
  identities, 0 quarantined. The in-file duplicates are explained, and
  three of them are rule fixes waiting for D18, led by Monthly Payments'
  key, which needs `transtype`
- **Not yet built**: the write side of the replay (replacing typed rows
  and re-curating from them, D18). Its queue: the Monthly Payments key,
  the three Curve money columns typed as text, two keys pending vendor
  answers (`docs/decision-record.md`, "The verification")

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

**A file's identity is its source plus its bytes.** Two specs can see one
workbook: Cardinal's summary and detail both match `cardinal.*invoice`, and
each recognises the other's shape with `loaded_by` (D16). Keyed on content
hash alone, the second spec would have reused the first's file record under
the wrong source key. `uq_source_file` carries `source_key`, and a duplicate
is a duplicate for one source.

**A file past its quarantine rate is held, not loaded empty.** PRxP's
2026-10 pulls quarantined 100% of their rows when a vendor column changed
shape. Committed as `LOADED` with nothing behind them, their hashes would
have been duplicates forever to the corrected spec. `max_quarantine_rate`
(5% by default) holds the file with its hash still loadable (D17).

**A dollar or percent sign in a header is a word.** Header matching folds
punctuation, and the pricing workbook has `margin_$` beside `margin_%`;
folded to `margin` twice, the file was unreadable (2026-10-23). The two
signs now normalise to `dollar` and `pct` on both sides of a match, so
McKesson's `Extended Price ($)` still matches its own spec.

**A text value wider than its column fails the row, never the file.** A
2,620-character patient note failed the first patients load at insert,
because the parser enforced a length only when the spec declared one. The
default width (`transforms.values.TEXT_LENGTH`, 1000) is now the parser's
limit as well as the DDL's, so an over-long value quarantines as `TOO_LONG`;
a spec that needs more declares `max_length` up to 4000, and the migration
widens a live column to it, the one alteration it makes.

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
content hash this source already committed, a load that is not ok, a
quarantine rate past the spec's limit, no as-of, an as-of floor
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
              in-memory store behind one interface; backfill, the loop
              over a root; retype, the replay of typed from landed
  acquire/    the upload container: list, mirror into the landing root,
              record in meta.blob
  observe/    source freshness, the open items in meta and stg, and mail
  store/      config, connection, ledger schema
specs/        21 source specs (YAML) — generated once, hand-edited since
tools/spec_build/  the generators that produced them, from real headers
scripts/      migrate, audit_files, bind_real_files, scaffold_spec, layer_walk,
              trace_file, commit_file, backfill, acquire, freshness, retype,
              close_runs
```

## Running things

```bash
python -m pytest                    # 512 tests, no database
python -m ruff check . && python -m ruff format .

python scripts/bind_real_files.py   # bind every spec against the real extracts
python scripts/audit_files.py       # structural audit of every file
python scripts/migrate.py --env-file <path>   # apply schema (idempotent)
python scripts/commit_file.py <spec key> "<file>" --dry-run   # the engine, in memory
python scripts/commit_file.py <spec key> "<file>" --env-file <path>   # commit to Azure
python scripts/backfill.py --dry-run --only <spec key>        # every delivery, in memory
python scripts/backfill.py --env-file .env --actor <you> --fast [--only ...]   # to Azure
python scripts/acquire.py --env-file .env --actor <you> [--poll 300]   # fetch uploads, then load them
python scripts/verify_load.py --env-file .env         # the database against the raw files; exit 1 on a gap
python scripts/freshness.py --env-file .env [--email]   # is every source current; exit 1 on an alert
python scripts/retype.py --env-file .env --only <spec key>   # what a spec edit does to loaded data
python scripts/close_runs.py --env-file .env [--apply]       # runs a dead process left open
```

Always pass `--fast` for a real load: the batched path is the proven one and
the row-at-a-time path takes minutes per thousand rows. A dry run of every
source at once holds every row in memory; run `--only` one source at a time.
Every run ends with the files under the root that no spec claims; a vendor
file renamed past every pattern shows up there and nowhere else.

After editing a spec, run `retype.py` on that source before loading any
file: it says, line by line, what the edit does to the data already in
`typed` (rehashed, newly quarantined, newly typed) without reading a file.
It writes nothing (D18).

The vendor extracts are PHI and live outside this repository. Point at them
with `SBH_SOURCE_ROOT`.

## How files arrive

Decided 2026-10-07. Files are downloaded by hand from each vendor portal and
uploaded to Azure Storage every Sunday, with ad hoc midweek re-uploads when
data needs correcting. The ETL must pick up a midweek file without waiting for
Sunday. So:

- The acquire step (`scripts/acquire.py`, built 2026-10-23) lists the
  `sbh-data-bank` container of the `sbhsftp01` storage account, which the
  team fills over SFTP, copies every blob version it has not seen into a
  local landing root under the blob's own path, records it in `meta.blob`,
  and runs the backfill loop over the landing root. `--poll N` repeats
  that every N seconds. It is never a Sunday cron. The upload convention
  is a dated folder per download day, `inbound/MMDDYYYY/`, which is what
  gives each delivery its as-of; a corrected re-pull goes in a new dated
  folder, never over the old file. The ETL reads the container through
  the Blob API as an Entra ID identity (Storage Blob Data Reader on the
  container), never over SFTP and never with the account key.
- `expected_cadence_days` is a staleness threshold ("alert if nothing seen in
  N days"), not a schedule. Specs keep 7 for weekly sources; the freshness
  check (`observe/freshness.py`, built 2026-10-22) adds two days' grace so
  a Sunday-morning check does not fire before the upload, measures silence
  from the as-of of the newest **loaded** delivery (a held file is an open
  refusal, not a delivery), and never alerts on a discontinued source or
  one with no cadence.
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

The same file carries `SBH_BLOB_ACCOUNT` and `SBH_BLOB_CONTAINER` for the
acquire step (plus `SBH_BLOB_SAS` for a developer machine without the data
role; otherwise `DefaultAzureCredential`, which is `az login` locally and
the managed identity in production), and `SBH_SMTP_HOST`, `SBH_SMTP_PORT`,
`SBH_SMTP_USER`, `SBH_SMTP_PASSWORD`, `SBH_MAIL_FROM` and `SBH_ALERT_TO`
for the alerts. Every settings object excludes its secret from `repr`.

## Open questions for the business

These are in `docs/data-findings.md` with the evidence. They block
reconciliation, not ingestion.

1. **The `Class` → `Class_New` cutover date** in the Master COGS List. 28 of 121
   accounts were reclassified with no date recorded. Entity resolution reports
   `UNRESOLVED` for those until there is one.
2. **Which McKesson report is the purchase source.** Both parse to identical
   extended price, gross purchases and net quantity — loading both doubles every
   McKesson figure. Ordered quantity differs by 23 units.
3. **Answered 2026-10-23: `Monthly Payments` is the posting ledger of
   `Curve_Monthly Services`**, not a copy of it. Services is one row per
   charge line with its totals; Payments is two to twelve rows per charge
   line (transfers, payments, adjustments, refunds), and the payment rows
   sum to the service line's paid total on 94.6% of lines. Both are
   needed; reconciliation takes charges from one and cash from the other
   (`docs/data-findings.md`, 17).
4. **Four vendor asks** that would each remove a real problem: an order id from
   Allscripts, an appointment id from Curve, a line-sequence number on Curve
   Monthly Services, the encounter id on Athena Encounters.
