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

Python namespace package. Working name `recon340` — cheap to change now, painful later, so
settle it before Phase A starts.

### Tier 0 — Foundation

**`recon340.core`** — Tenant context as an explicit, required parameter rather than an ambient
global, so a query that forgets tenant scoping does not compile. Effective-dated config
resolution (`resolve(key, tenant, as_of)`). Run and batch identity. Audit trail. Injected clock
for deterministic replay. Typed errors.

**`recon340.store`** — Azure SQL access, forward-only migrations, and the ledger primitives:
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

### Tier 1 — Configuration and master data

**`recon340.registry`** — The source spec model. This is the centrepiece of configurability and
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

**`recon340.masterdata`** — Tenant, covered entity, site, wholesaler and TPA accounts, and
crucially **effective-dated program enrollment**. Also the NDC dimension, versioned HCPCS/ASP
crosswalk snapshots, drug scope (Part B vs Part D), chart of accounts, and the period calendar.

Reference data that is federal (NDC directory, ASP crosswalk) lives under a `system` tenant and
is inherited; tenant-specific data (account mappings, tolerances) overrides it. That split is
what keeps a second customer cheap.

### Tier 2 — Ingestion

**`recon340.ingest`** — Acquire (blob, SFTP, upload) → identify → read → type → validate →
quarantine → commit as one batch. Keep three ideas from `staging_pipeline.py`, which are the
best parts of the recovered source: the single-transaction raw + quarantine + promote commit,
the quarantine threshold abort, and SHA-256 file dedup keyed on `(name, hash, status)`. Drop
the rest.

Two operational fixes the logs make urgent: an expected-source manifest, so "Cardinal has not
delivered in 8 days" is an alert rather than a `WARNING` nobody reads; and a run summary that
counts inserts *and* supersessions, since today `rows_promoted` excludes MERGE updates and
therefore reported `promoted=0` on runs that were working.

### Tier 3 — Canonicalization

**`recon340.canonical`** — The seam that makes the product multi-entity. Vendor records become
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

**`recon340.expectations`** — The DB_Validation_Pack's 138 checks become declarative rules that
run on every batch, with results stored as first-class data. Completeness, uniqueness, validity,
referential integrity and reconciliation, per source and per canonical type. Validation stops
being a consulting engagement and becomes a property of the pipeline.

### Tier 5 — Reconciliation

**`recon340.match`** — One generic, declarative match engine serving both 340B and accounting
reconciliation. A match spec declares left dataset and filter, right dataset and filter, grain
(entity, site, NDC, period), an **ordered list of passes** (exact key → tolerance → windowed
fuzzy), the measure being compared with its unit and conversion, and the outcome taxonomy with
reason codes. Explanation records are always produced.

Keep the plugin-registry pattern from `engine.py` — it is genuinely well factored. Discard the
`ReconciliationConfig` knobs that do nothing: `date_window_days` is parsed and never used,
`cogs_method` declares FIFO but only average-cost SQL exists, and `include_buybill_revenue`
emits a SQL comment instructing an operator to hand-edit a view.

**`recon340.inventory`** — 340B inventory, accumulator and replenishment reconciliation, grained
by entity and site rather than pooled globally. Resolves the double-subtraction risk in
`purchased − dispensed − claimed` by classifying each canonical event to a channel explicitly,
so a physical dispense cannot be counted on both sides.

**`recon340.financial`** — The four accounting reconciliations from the roadmap (wholesaler
invoices vs AP/GL, TPA remittances vs bank/GL, inventory valuation and COGS to GL, 340B savings
and true-up), the period model with immutable closed periods, and the close pack. GL, AP and
bank exports are ordinary source specs with drift detection.

### Tier 6 — Operations and delivery

**`recon340.cases`** — Unified exception management for both 340B and accounting exceptions:
reason codes, assignment, resolution, SLA, full audit. Replaces the discrepancy log whose dedup
guard prevents re-raising.

**`recon340.orchestrate`** — Run graph with dependencies, retries, freshness and missed-run
detection. Azure Container Apps jobs rather than a WebJob plus a Flask keep-alive; the
unauthenticated `POST /run` endpoint does not carry over.

**`recon340.api`** (FastAPI) and **`webapp`** (React) — Entra ID SSO, roles from security
groups. Screens per the roadmap, plus two that are specific to this design: the simulation diff
view and the source-onboarding wizard.

**`recon340.observe`** — Metrics, alerts, source freshness.

## Multi-tenancy

`tenant_id` non-null on every table from the first migration. Azure SQL row-level security as
defence in depth behind app-level filtering. Tenant context threaded explicitly through `core`.
Per-tenant Key Vault secrets and blob containers. Config rows tenant-scoped with `system`-tenant
inheritance. Secrets never in files — the recovered `settings.yaml` carries a plaintext SQL
admin password, which is the posture being replaced.

## Sequencing

Breadth-first per the decision taken. Roughly 18 weeks at focused pace.

**Phase A — Foundation, weeks 1–2.** New database and migrations. Ledger primitives. Tenant,
CE and site model. Source spec registry and transform vocabulary. NDC dimension and correct
normalization. Expectations engine. Cardinal end to end, file to canonical `PurchaseLine`, with
a golden fixture. The skeleton is complete; it simply knows one source.
*Gate:* Cardinal loads, reloads idempotently, and a deliberately corrupted file quarantines
cleanly.

**Phase B — All 18 sources, weeks 3–6.** A spec per source, each with a golden fixture and
expectations, replaying full history from the files already on disk. Versioned ASP crosswalk
and account reference loads.
*Gate:* every source lands typed, entity-resolved and reproducible, with a per-source quality
report. Re-running the whole history changes nothing.

**Phase C — Master data completeness, weeks 5–7 (overlaps B).** Site-resolution rules including
shared accounts. Effective-dated program enrollment. UoM conversion. Part B / Part D scope.
*Gate:* the 340B verification plan's assertions pass — zero matched activity on
Southington-only accounts after 2025-07-17, no shared-account inventory attributed to the CT
site after that date, Nevada and Orangeburg disambiguated by WeInfuse `location_id`.

**Phase D — 340B inventory reconciliation, weeks 7–10.** Match engine, inventory reconciliation,
explanation records, simulation. Working sessions with you as the 340B authority.
*Gate:* one month and a chosen NDC set match a hand reconciliation, signed off in writing.

**Phase E — Financial reconciliation and close, weeks 10–13.** GL, AP and bank specs. Four match
specs. Period model and close pack.
*Gate:* one real month-end close produced by the pipeline with every unmatched item reasoned.

**Phase F — Product layer, weeks 12–18 (overlaps E).** API, app, onboarding wizard, simulation
UI, tenant provisioning.
*Gate:* three staff who do not write SQL complete a weekly review unaided.

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

- Product and namespace name — settle before Phase A.
- Repository location and hosting (GitHub organization owned by Sagebrush).
- Azure subscription and resource group for the new database and Container Apps environment.
- CSD / CuraScript is not ingested and is the cleanest site disambiguator for Nevada versus
  Orangeburg. Worth requesting the extract; the design does not depend on it, but resolution
  quality improves materially with it.
- Whether Power BI remains the analytics surface on top of the new models.
