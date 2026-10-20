# 340B Reconciliation Platform — architecture and build plan

## Context

Sagebrush needs a reconciliation product, not an ETL pipeline. The consultant's recovered
Python codebase (`Azure App ETL Files - Consultant/recovered-source`) is a reasonable first
draft of an ingestion framework, but the exploration found that it has never delivered
production data and that its reconciliation logic is structurally unable to answer the
question the business actually asks.

What the evidence shows:

- **Ingestion has never worked.** Every logged run from 30 Jun to 21 Jul 2026 reports
  `promoted=0`. Sixteen of eighteen sources never matched their glob pattern — logged as
  `WARNING`, while the WebJob reported `"Status":"Success"` every time. The two that matched
  were a duplicate skip and the same 1,183 rows re-upserted. All data in `pharmacy_qa` came
  from 19 hand-run scripts that bypass staging, validation, dedup and NDC normalization.
- **NDC normalization is wrong.** Both `RIGHT('00000000000' + REPLACE(ndc,'-',''), 11)` in SQL
  and `zfill(11)` in Python are naive left-pads, correct only for 5-4-1 NDCs and silently wrong
  for 4-4-2 and 5-3-2. The SQL version runs for only 2 of 9 sources. Every cross-source join in
  the system is an exact string match on that column.
- **There is no entity dimension in reconciliation.** `vw_inventory_reconciliation` pools every
  covered entity, clinic and wholesaler account into one global `(ndc, week)` bucket. It cannot
  answer "what is unaccounted for NDC X at Fire Mesa."
- **No unit-of-measure conversion.** Wholesalers ship packages; WeInfuse records HCPCS billing
  units. `unaccounted_qty = purchased − dispensed − claimed` subtracts them directly. The 130
  NDCs the validation pack flagged as over-dispensed are probably mostly this bug.
- **Eight of nine fact tables have no unique constraint**, and backfill scripts include a
  `DELETE ... WHERE enc_date BETWEEN ?` pattern that can destroy unrelated rows.
- **15,207 open discrepancies**, and the `NOT EXISTS (ndc, week_start)` dedup guard means a
  discrepancy can never be re-raised once logged — even after resolution, even if its magnitude
  changes on the next refresh.

Two roadmap items are now closed by direct evidence: **Cardinal does carry NDC** (the
`Invoice Review` export has `NDC`, `CIN` and `Invoice Line #`), so no report change is needed;
and **sample files for all 18 sources already exist** on disk back to Jan 2025, so Phase 1 is
not gated on file collection.

The intended outcome is a modular, configuration-driven product that produces numbers a
covered entity can defend in an HRSA audit, deployable at a second organization without
engineering work.

## Decisions taken

| Decision | Choice |
|---|---|
| Codebase | New product. The consultant's source is reference material for column mappings and business rules; no code carries over. |
| Stack | Python 3.12 + Azure SQL. Transformations as versioned SQL models under our own migration tooling. |
| Database | Brand new database, fresh schema. No migration from `pharmacy_qa`. |
| Multi-entity | Multi-tenant from day one — the product will be sold to other covered entities. |
| Data model | Append-only bitemporal ledger. Nothing is updated or deleted. |
| First milestone | All 18 sources ingesting correctly before reconciliation is built. |
| PHI | BAA in force; raw vendor files may be read directly. |

## What makes this different from an ETL pipeline

Five commitments that ordinary ETL tools do not make. These are the product, and they are why
the module boundaries fall where they do.

**1. A business rule is a row, not a line of code.** "Southington stopped 340B on 2025-07-17"
is a `program_enrollment` row with `valid_to = 2025-07-17`, an author and an approval — not a
`WHERE` clause. Reconciliation resolves rules as-of the event date, so changing a rule today
does not silently rewrite last year's answer. Every rule that the 340B verification plan
identifies as volatile — account-to-site mappings, shared-account disambiguation, program start
and stop dates, Part B vs Part D scope, tolerances, period definitions — lives this way.

**2. Any past answer can be reproduced exactly.** The ledger records both when an event
happened and when we learned about it. Combined with versioned rules, `run(period, as_of=T)`
returns precisely what the system said at time T. That is the audit answer, and it is the
single hardest thing to retrofit — which is why it is Phase A.

**3. Every number explains itself.** A reconciliation output row carries an explanation record:
the input row identities on both sides, the rule versions applied, the unit conversions used
and their source, and the arithmetic. A $3,400 variance opens to four purchase lines, twelve
administrations, the `BILLUNITSPKG` multiplier that converted packages to billing units, and
the tolerance band that made it an exception. This is what gets non-technical staff to trust
the output instead of asking for a query.

**4. Configuration changes are reviewable before they are real.** Change a tolerance, an
account mapping or a conversion rule, replay it against history in a shadow run, and read the
diff: *reclassifies 47 exceptions, closes 31, opens 6, moves $118k.* Then approve. Config gets
the same rigour as code, without a deployment.

**5. Onboarding a source is a product feature, not an engineering ticket.** Drop a file, the
system profiles it, proposes a spec, a human confirms the mapping, a golden fixture is
captured, it goes live. This is the mechanism by which the second covered entity becomes a
customer rather than a project.

## Module architecture

Python namespace package `sbh_data_bank`; database `SBH_DATA_BANK`.

### Tier 0 — Foundation

**`sbh_data_bank.core`** — Tenant context as an explicit, required parameter rather than an ambient
global, so a query that forgets tenant scoping does not compile. Effective-dated config
resolution (`resolve(key, tenant, as_of)`). Run and batch identity. Audit trail. Injected clock
for deterministic replay. Typed errors.

**`sbh_data_bank.store`** — Azure SQL access, forward-only migrations, and the ledger primitives:
batch registration, row lineage, temporal views. Keep the connection-pool design from
`src/db/connection.py`; it is the best-engineered file in the recovered source (correct pyodbc
thread handling, liveness check, rollback-on-return). Extend it with tenant routing and
retry-with-backoff, which it lacks.

Ledger shape, applied to every fact table:

```
fact_<type>(
  tenant_id, ce_id, site_id,          -- always present, never null
  <business dimensions>, event_date,   -- valid time
  batch_id, ingested_at,               -- transaction time
  source_row_hash,                     -- identity
  superseded_by_batch_id, superseded_at,
  <measures>
)
```

Current state is `superseded_by_batch_id IS NULL`. As-of state adds
`ingested_at <= @as_of AND (superseded_at IS NULL OR superseded_at > @as_of)`. A re-delivered
file appends new and changed rows and supersedes vanished ones. Nothing is deleted, so the
delete-by-date-range class of bug cannot occur.

This ledger shape applies to the curated layer. The layers themselves, and which rules run
between them, are set out under "Storage layers" below.

### Tier 1 — Configuration and master data

**`sbh_data_bank.registry`** — The source spec model. This is the centrepiece of configurability and
must cover every axis of variation found across the 18 existing parsers:

- *Identity*: filename pattern plus a header-content fingerprint, so a renamed file still
  matches and a changed layout does not silently match the wrong spec.
- *Variants*: an ordered list with detector predicates. WeInfuse Administered has two live
  export schemas distinguished by the presence of an `Inventory Type` column; today that is
  hand-coded, and it must be declarative.
- *Read*: format, encoding, delimiter, `preamble_lines` (ABC has a `sep=,` line and a title
  line before the header), `header_row`, sheet selection (absent entirely today — every Excel
  read takes the default sheet), footer detection (also absent — a "Grand Total" row currently
  survives `dropna(how="all")` and becomes a record).
- *Columns*: each target field takes a **list** of acceptable source names with normalized
  matching. This kills the Allscripts `LOCATION_NAME1 | LOCATION_NAME` fallback special-case
  and the Wellpartner ``Days` Supply`` backtick typo, which currently works only because it
  exactly reproduces an error in the vendor's own header.
- *Drops*: patterns such as `Textbox*` (Curve SSRS artifact) and `Unnamed:*` (pandas index).
- *Constants*: arbitrary injected values. Today only Pharmaforce can do this, via a
  special-cased `location` key; it becomes generic, which is what lets one spec serve Fire Mesa
  and Southington.
- *Identity and dedup*: `natural_key`, `row_hash_columns`.
- *Canonical target*: type plus field map plus site-resolution strategy.
- *Drift policy*: what to do on a new or missing column — fail, warn, or quarantine. Never
  silently drop, which is the current behaviour.

Transforms are a named, tested vocabulary rather than per-parser helpers: `trim`,
`parse_number`, `parse_currency`, `parse_date(formats)`, `normalize_ndc`, `parse_bool`,
`nullify(["nan","n/a","null","-"])`, `regex_extract`. Three specific defects this closes: the
four byte-identical copies of `_clean`/`_i` scattered across parser modules; the latent bug
where `str(row.get(x, "") or "").strip()` stores the literal string `"nan"` in three of four
wholesaler parsers (NaN is truthy, so the `or ""` never fires); and `safe_date`'s `fmt`
parameter, which is declared but never passed, leaving every date to pandas' inference.

**`sbh_data_bank.masterdata`** — Tenant, covered entity, site, wholesaler and TPA accounts, and
crucially **effective-dated program enrollment**. Also the NDC dimension, versioned HCPCS/ASP
crosswalk snapshots, drug scope (Part B vs Part D), chart of accounts, and the period calendar.

Reference data that is federal (NDC directory, ASP crosswalk) lives under a `system` tenant and
is inherited; tenant-specific data (account mappings, tolerances) overrides it. That split is
what keeps a second customer cheap.

### Tier 2 — Ingestion

**`sbh_data_bank.ingest`** — Acquire (blob, SFTP, upload) → identify → read → type → validate →
quarantine → commit as one batch. Keep three ideas from `staging_pipeline.py`, which are the
best parts of the recovered source: the single-transaction raw + quarantine + promote commit,
the quarantine threshold abort, and SHA-256 file dedup keyed on `(name, hash, status)`. Drop
the rest.

Two operational fixes the logs make urgent: an expected-source manifest, so "Cardinal has not
delivered in 8 days" is an alert rather than a `WARNING` nobody reads; and a run summary that
counts inserts *and* supersessions, since today `rows_promoted` excludes MERGE updates and
therefore reported `promoted=0` on runs that were working.

**How files arrive (decided 2026-10-07).** Extracts are downloaded by hand from each
vendor portal and uploaded to Azure Storage every Sunday. A source may also be
re-pulled and uploaded midweek when data needs correcting, and the ETL must pick that up
without waiting for the next Sunday. Three consequences for the design:

- *Acquire is event- or poll-driven, not a Sunday cron.* The acquire step watches the
  blob container on a short interval or reacts to blob-created events. The Sunday upload
  is an operating habit, not a contract the pipeline depends on.
- *`expected_cadence_days` is a staleness threshold, not a schedule.* It means "alert if
  no new file for this source has been seen in N days". A midweek upload resets the
  clock early, which is harmless. Specs keep the true expectation (7 for weekly sources);
  the alerting engine adds a grace period so a check that runs Sunday morning before the
  upload does not raise a false alarm.
- *Overlapping deliveries are the normal case.* A midweek re-pull usually covers a wider
  date range than Sunday's file. Content-hash file identity makes an identical re-upload
  a no-op. Which of two differing deliveries wins is decided by as-of date, not upload
  order; the rule is under "Duplicates and delivery order" below.

### Tier 3 — Canonicalization

**`sbh_data_bank.canonical`** — The seam that makes the product multi-entity. Vendor records become
`PurchaseLine`, `AdministrationEvent`, `ClaimEvent`, `RemittanceLine`, `GLEntry`,
`PatientEncounter`. Reconciliation is written once against canonical types, so a new wholesaler
is a new spec and zero reconciliation changes. Three hard problems live here:

*NDC normalization, done correctly.* The FDA 10-digit formats are 4-4-2, 5-3-2 and 5-4-1, and
the format is **not recoverable from an unhyphenated 10-digit string**. Hyphenated input: count
segments, insert the leading zero into the deficient segment. Eleven digits: accept. Ten digits
unhyphenated: try all three expansions against the NDC dimension; accept a unique hit, and
quarantine with reason `NDC_AMBIGUOUS` on zero or multiple. Store `ndc11`, `ndc_raw` and
`ndc_resolution_method` so the guess is always visible. This resolves the 335 ABC and 42
Pharmaforce NDCs the validation pack flagged, and is honest about any it cannot.

*Unit-of-measure conversion.* Purchases arrive in packages, administrations in HCPCS billing
units. Conversion is `packages × BILLUNITSPKG` from the ASP crosswalk version in force on the
transaction date. Both native and converted quantities are stored alongside the factor and its
source version. No comparison may cross units without an explicit conversion.

*Entity and site resolution.* The 340B ID (`OPAID` / `Associated 340B ID` / `340B ID`, e.g.
`STD891281`) is the natural key and appears in Pharmaforce, McKesson Custom and Wellpartner.
ABC, Cardinal and WeInfuse have no such column and resolve through effective-dated account
mappings — but ABC account `0100583154` is shared across all three clinics and Cardinal
`2057234776` across two, so account number alone is insufficient. Resolution is an ordered,
configured strategy list per source, terminating in an explicit `UNRESOLVED` outcome rather
than a guess. This is precisely the rule the 340B verification plan says the consultant's view
must apply and may not.

### Tier 4 — Quality

**`sbh_data_bank.expectations`** — The DB_Validation_Pack's 138 checks become declarative rules that
run on every batch, with results stored as first-class data. Completeness, uniqueness, validity,
referential integrity and reconciliation, per source and per canonical type. Validation stops
being a consulting engagement and becomes a property of the pipeline.

### Tier 5 — Reconciliation

**`sbh_data_bank.match`** — One generic, declarative match engine serving both 340B and accounting
reconciliation. A match spec declares left dataset and filter, right dataset and filter, grain
(entity, site, NDC, period), an **ordered list of passes** (exact key → tolerance → windowed
fuzzy), the measure being compared with its unit and conversion, and the outcome taxonomy with
reason codes. Explanation records are always produced.

Keep the plugin-registry pattern from `engine.py` — it is genuinely well factored. Discard the
`ReconciliationConfig` knobs that do nothing: `date_window_days` is parsed and never used,
`cogs_method` declares FIFO but only average-cost SQL exists, and `include_buybill_revenue`
emits a SQL comment instructing an operator to hand-edit a view.

**`sbh_data_bank.inventory`** — 340B inventory, accumulator and replenishment reconciliation, grained
by entity and site rather than pooled globally. Resolves the double-subtraction risk in
`purchased − dispensed − claimed` by classifying each canonical event to a channel explicitly,
so a physical dispense cannot be counted on both sides.

**`sbh_data_bank.financial`** — The four accounting reconciliations from the roadmap (wholesaler
invoices vs AP/GL, TPA remittances vs bank/GL, inventory valuation and COGS to GL, 340B savings
and true-up), the period model with immutable closed periods, and the close pack. GL, AP and
bank exports are ordinary source specs with drift detection.

### Tier 6 — Operations and delivery

**`sbh_data_bank.cases`** — Unified exception management for both 340B and accounting exceptions:
reason codes, assignment, resolution, SLA, full audit. Replaces the discrepancy log whose dedup
guard prevents re-raising.

**`sbh_data_bank.orchestrate`** — Run graph with dependencies, retries, freshness and missed-run
detection. Azure Container Apps jobs rather than a WebJob plus a Flask keep-alive; the
unauthenticated `POST /run` endpoint does not carry over.

**`sbh_data_bank.api`** (FastAPI) and **`webapp`** (React) — Entra ID SSO, roles from security
groups. Screens per the roadmap, plus two that are specific to this design: the simulation diff
view and the source-onboarding wizard.

**`sbh_data_bank.observe`** — Metrics, alerts, source freshness.

## Multi-tenancy

`tenant_id` non-null on every table from the first migration. Azure SQL row-level security as
defence in depth behind app-level filtering. Tenant context threaded explicitly through `core`.
Per-tenant Key Vault secrets and blob containers. Config rows tenant-scoped with `system`-tenant
inheritance. Secrets never in files — the recovered `settings.yaml` carries a plaintext SQL
admin password, which is the posture being replaced.

## Storage layers (decided 2026-10-07)

The first schema built for this product landed typed, deduplicated rows directly into
`raw.*` tables. That put three business decisions - which rows parse, which columns identify
a row, and which rows supersede which - ahead of the first database write. If any of those
rules turned out wrong, the only recovery was to re-read the vendor files, and the database
never held the evidence of what a vendor actually sent. The McKesson natural key, declared
unique and found on inspection to collide on 11 of 6,163 rows, is the case that showed the
cost.

The decision is a four-layer design in the medallion pattern (bronze, silver, gold), with
the gold layer split into curated and presentation because the curated ledger and the views
users query change for different reasons.

| Layer | Schema | Content | Rebuilt from | Changes when |
|---|---|---|---|---|
| Landed | `landed` | Every cell as delivered text, one row per file line, plus file id, line number, delivery rank, ingested_at | Files, once | Never |
| Typed | `typed` | Parsed values per the spec; quarantine for what will not parse; row hash | Landed | A parsing rule changes |
| Curated | `curated` | Dedup policy, supersession, site resolution, canonical events, under a rule version | Typed | A business rule changes |
| Presentation | `pres` | Views and marts for users and for reconciliation | Curated | Reporting needs change |

Rules that follow from it:

- **Landed is immutable and as-delivered, with one exception.** Prohibited columns (SSN in
  Monthly Payments and Curve Monthly Services) are matched for drift detection and dropped
  before landing. This is the minimum-necessary standard applied once, at the boundary, and
  it is the only deviation from "exactly what the vendor sent". Direct identifiers that do
  land are encrypted at rest with column-level access control from the landed layer onward.
- **Nothing is dropped before landed.** Duplicate files are refused by content hash; that is
  the only filter. Footer rows, blank rows and rows that will later fail typing all land.
  Typing and quarantine happen landed-to-typed; dedup happens typed-to-curated.
- **Every step between layers is a versioned rule that can be re-run from the layer below.**
  Changing a natural key, a date format or a dedup policy is a re-run over data already in
  the database, with the prior result kept under its rule version. No step re-reads files.
- **The spec is still the single definition.** One source spec drives all three steps: how
  to land (read settings), how to type (columns and formats), and how to curate (natural
  key, dedup, site resolution). Nothing in the spec format changes.
- **The existing `raw.*` tables are the typed layer under an old name.** They are renamed
  rather than redesigned; the ledger columns they carry move to curated, and typed keeps
  only file id, line number and row hash.
- **Storage is the accepted cost.** Overlapping weekly deliveries land in full. Curve Monthly
  Services is 260 MB per drop and Monthly Payments over a million rows; that is budgeted
  for, because it is the price of showing what each delivery contained.

Consequence for the build order: the commit engine (built 2026-10-20, `commit/`) is built
against these four layers from the start. The McKesson key question moves from "decide
before loading" to "a curated-layer rule that can be changed and re-run".

## Duplicates and delivery order (decided 2026-10-15)

The first schema carried three dedup policies (natural key, row hash, file position) because
three sources defeated the first two. The decision is one rule for every source, so that a
reader of any table reasons about duplicates the same way, with the source-specific part
reduced to a list of columns in the spec.

**Identity.** A row's identity is its identity columns plus an occurrence number. The
identity columns are the spec's `natural_key`; a spec without one uses every landed column.
Within one delivery, rows that share identity columns are numbered in file order. The number
is part of the identity, so nothing within a file is ever collapsed: the two McKesson
split-shipment lines that are identical in every identifier become occurrence 1 and 2, and
the 8,100 byte-identical Allscripts eRx prescriptions are 8,100 identities. The known
limitation is order dependence: if a vendor reorders two colliding lines between deliveries,
the pipeline records a correction that is cosmetic. The three McKesson deliveries on disk are
the test for whether that happens.

**What dedup is not.** It never decides which business line is right. Probed on the McKesson
history file: all 31 credit and return lines carry their own credit number, distinct from the
original invoice number, and none shares an invoice-plus-item key with a purchase line. A
return is a new identity, inserted beside the original purchase; reconciliation sums the two.
An order of 8 followed by a return of 3 is two live rows totalling 5, never one row of 3.

**As-of.** Every delivery carries an as-of date: when the vendor extracted it. Supersession
is ordered by as-of, never by arrival, because an operator can upload a months-old download
after a newer one and the newer data must stand. Where as-of comes from, in order:

0. Decided later (2026-10-16, D5 in `docs/decision-record.md`): the dated upload folder
   every Sunday upload goes into, for every source. It is tried before everything below,
   which remains the chain for the backlog files that have no such folder.
1. The workbook's internal creation stamp (`docProps/core.xml`). Probed on all 32 real
   xlsx files: the vendor portals write it (SAP for ABC, Apache POI for Cardinal and WeInfuse,
   Kendo for Wellpartner) and it is the extraction date. ABC's file named through April 5 was
   created April 30, which is exactly the late-download case the stamp is for. Two files
   lack it.
2. The end date of the range in the filename. Every real file carries one. This is the only
   option for CSV, which is 110 of the 142 real files, including every McKesson file.
3. A date the operator supplies at upload.

A file with none is refused rather than guessed. The as-of must not precede the latest event
date in the file, or the batch fails. Filesystem timestamps are not used: every file in a
drop shows the day it was copied to OneDrive. Upload time is recorded as arrival time for
audit and plays no part in ordering.

**Outcomes.** For each incoming row, compared with the current row of the same identity:

| Incoming vs current | Outcome | Counted as |
|---|---|---|
| No current row | Insert | inserted |
| Newer as-of, same hash | Nothing | unchanged |
| Newer as-of, different hash | Insert; supersede current | corrected |
| Older as-of | Insert as history behind the current row; current unchanged | late |
| Same as-of, different hash | Batch held for a person | conflict |
| Current row absent from a newer delivery covering its date | Supersede current | vanished |

A late batch is not an error. Its rows reach landed, typed and curated in full, and improve
the answer to "what was known on that date", but they never become current. The batch report
states how many rows were late so the operator learns that an old file was uploaded.

**Where it lives.** `store/schema.py::DEDUP_RULE` names the rule version recorded on every
batch and every curated row; `identity_columns_for(spec)` gives the identity columns. The
curated DDL carries `identity_hash`, `occurrence`, `as_of`, `outcome` and `supersede_reason`;
`meta.source_file` carries `as_of` and `as_of_source`; `meta.batch` carries a counter per
outcome. The commit engine (`commit/engine.py`, 2026-10-20) fills them; its own refusals
and the engine-level decisions are in `docs/decision-record.md`, "The commit engine".

## Scope boundary: what counts as "the ETL"

The instruction is to finish the ETL, have it be production ready, and never come back to
it. That is achievable, but only if three things that sound like reconciliation are built
inside the ETL rather than after it. Each one, deferred, forces a return trip that
reprocesses every row already loaded.

**1. Entity and site resolution belongs to the ETL.** This is the defect that sank the
previous system: `vw_inventory_reconciliation` pools every covered entity and clinic into
one global `(ndc, week)` bucket because nothing upstream ever resolved a row to a site.
Resolution depends on effective-dated account mappings, on WeInfuse `location_id`, and on
shared accounts that cannot be attributed from an account number alone. Adding that later
means re-deriving it for every historical row. It is cheap now and expensive later, so it
is in scope.

**2. The canonical event model belongs to the ETL.** If the ETL lands only per-vendor raw
tables, then building reconciliation later necessarily changes the schema, which means
reprocessing. Landing `PurchaseLine`, `AdministrationEvent`, `ClaimEvent`,
`RemittanceLine`, `PatientEncounter` now means reconciliation is later written against a
stable contract and the ETL is genuinely finished.

**3. The inputs to unit conversion must be captured at load.** Conversion itself is
reconciliation work and is deferred. But package size, billing units, HCPCS code, UOM,
strength and the ASP crosswalk version in force on the transaction date have to be
captured and versioned *at load time*, because they are not recoverable afterwards from a
row that dropped them. Capture now, convert later.

Everything else that is genuinely reconciliation - the match engine, inventory
reconciliation, the four accounting matches, period close, discrepancy triage - is out of
scope and comes after.

## Definition of done for the ETL

Not a status report. Every line below is a check that runs, and the ETL is not finished
until all of them pass.

### Correctness of what lands

- Every destination column is typed from the typed layer onward. Text catch-alls exist only
  in the landed layer, which is as-delivered by design.
- NDC normalized with `ndc11`, `ndc_raw` and `ndc_resolution_method` on every row;
  ambiguous and lossy values quarantined rather than guessed.
- Dates parsed with explicit per-source formats. Nothing left to pandas inference.
- Money parsed to `DECIMAL`. No currency strings, thousands separators or `$` survive.
- No literal `"nan"`, `"none"`, `"null"` or `"N/A"` string in any column, anywhere.
- Quantities stored with their unit, alongside package size and billing-unit factors.
- Every row carries tenant, covered entity, site, source file, batch, spec version and
  row hash, or an explicit `UNRESOLVED` marker with a reason.

### Behaviour under replay

- Reloading any single file changes nothing.
- Replaying the entire history twice produces a byte-identical current state.
- A corrected re-delivery supersedes the prior version; it does not duplicate it.
- Late-arriving data lands without altering any previously reported answer.

### Completeness

- All 18 sources plus the reference loads, each with a golden fixture running in CI.
- Per-load control totals reconciled against the source file: row count in, rows landed,
  rows quarantined, and a summed money column where the file has one. A load that cannot
  account for every row it read fails.

### Failure handling

- Every rejected row is in quarantine with reason codes and its full payload.
- Quarantine is reprocessable after a fix without re-running the whole file.
- One bad row never fails a file. One bad file never fails a run.
- Schema drift - a new or missing vendor column - is reported and actioned per policy,
  never silently dropped.

### Operations

- No secret in any file. Key Vault with managed identity.
- CI runs lint, unit tests and every golden fixture on each pull request.
- Deployment is repeatable from source (infrastructure as code).
- Alerts fire on: run failed, expected source missing for N days, quarantine rate spike,
  control-total mismatch. The six silent missed runs in the old system are the bug this
  closes.
- A runbook exists for each alert.

### Multi-tenancy

- `tenant_id` non-null and enforced on every table.
- Onboarding a second covered entity requires configuration only, demonstrated by
  actually doing it for a synthetic tenant in CI.

## Sequencing

Reconciliation deferred. Roughly 10 weeks to a finished ETL.

**Phase A - Foundation, weeks 1-2.** New `SBH_DATA_BANK` database and migrations. Ledger
primitives. Tenant, covered entity and site model. Source spec registry and transform
vocabulary. NDC dimension and normalization. Expectations engine. Cardinal end to end,
file to canonical `PurchaseLine`, with a golden fixture.
*Gate:* Cardinal loads, reloads idempotently, and a deliberately corrupted file
quarantines cleanly.

**Phase B - All 18 sources, weeks 3-6.** A spec per source with a golden fixture and
expectations, replaying full history from the files on disk. Versioned ASP crosswalk and
account reference loads.
*Gate:* every source lands typed and reproducible with a per-source quality report;
replaying the whole history twice changes nothing.

**Phase C - Entity resolution and canonical completeness, weeks 5-8 (overlaps B).**
Site-resolution strategies including shared accounts. Effective-dated program enrollment.
Conversion inputs captured. Part B / Part D scope flags.
*Gate:* the 340B verification plan's assertions hold at the data layer - no purchase on a
Southington-only account resolves to Southington after 2025-07-17, and Nevada and
Orangeburg are separable on shared ABC and Cardinal accounts.

**Phase D - Production hardening, weeks 8-10.** Secrets, CI, infrastructure as code,
deployment, alerting, runbook, operator CLI for quarantine and replay. The second-tenant
onboarding drill.
*Gate:* every line of the definition of done above passes.

**Then, and only then, reconciliation.** The match engine, 340B inventory reconciliation,
the four accounting reconciliations, period close and the product layer - planned
separately once the ETL is signed off.

## Verification

Every phase carries executable proof, not a status report.

- **Golden fixtures** — one de-identified sample per source with expected row counts and
  checksums, run in CI. A vendor layout change fails the build.
- **Idempotency test** — replay the entire history twice; the ledger's current state must be
  byte-identical. This is the property the old pipeline could not hold.
- **Reproducibility test** — run a reconciliation, ingest a late-arriving correction, re-run
  `as_of` the original timestamp, and assert the original answer returns unchanged.
- **Rule-change simulation test** — move a tolerance, assert the diff report matches a
  hand-computed expectation.
- **Business-rule assertions** — the 340B verification plan's Step 3 checks, as automated tests
  rather than one-off queries.
- **Hand reconciliation** — the Phase D gate. One month, NDC by NDC, against your own numbers.
  Nothing else substitutes for this.

## Open items

- Repository location and hosting (GitHub organization owned by Sagebrush).
- Azure subscription and resource group for the new database and Container Apps environment.
- CSD / CuraScript is not ingested and is the cleanest site disambiguator for Nevada versus
  Orangeburg. Worth requesting the extract; the design does not depend on it, but resolution
  quality improves materially with it.
- Whether Power BI remains the analytics surface on top of the new models.
