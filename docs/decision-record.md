# Decision record

One page. Every decision the ETL engine depends on, with the date, who made
it and where the evidence sits. Newest at the bottom. Earlier design
decisions (storage layers, the dedup rule) are in
`docs/architecture-and-plan.md`; this file starts where the layer walk
(`docs/layer-walk/README.md`, decisions D1 to D10) handed questions to the
developer on 2026-10-16.

## How decisions are split (2026-10-16)

Agreed with the developer, who owns the project and reviews every change:

- **Business decisions** are taken by the developer, one source at a time,
  when that source is loaded. Each source's open questions are in its
  `docs/layer-walk/<key>.analysis.md`.
- **Design defaults** are taken by the engine author from the layer-walk
  evidence and recorded here. They are reviewed when the source that uses
  them comes up. Every one is a versioned rule re-run from the typed layer,
  so a wrong default is a re-run in the database, never a re-read of files.
- **Presentation decisions** (which report feeds a figure, what grain a view
  counts) wait until views are built. Nothing about loading depends on them.

## The download routine (2026-10-16, developer)

Every Sunday, for every source, the last three weeks of data are downloaded,
so each new file overlaps the previous one by about two weeks. Overlap is
therefore the normal case, not the exception, and the dedup rule must treat
a re-delivered row as unchanged or corrected without any operator action.
The history files already on disk (142 files, 2023 to June 2026) were pulled
ad hoc and do not follow this routine.

## D1. Event date is declared in the spec (2026-10-16, developer: implement)

`event_date: <target>` on SourceSpec, naming the column that says when the
business event happened (invoice date, fill date, appointment time). Snapshot
sources (eligibility, wholesaler accounts, patients) declare `as_of`. Used by
the as-of floor, period placement, coverage and vanish. Each source's value
is the "proposed event date" column of the layer-walk scorecard.

## D2. Coverage per source (2026-10-16, default)

`coverage: filename | data | as_of` plus `coverage_start_offset_days` and
`coverage_end_offset_days`, and an optional `coverage_column` where the
vendor filters on a column other than the event date (Wellpartner claims:
`processed_date`). Defaults from the walk: Cardinal ends at as-of minus one
day; WeInfuse starts one day before the filename; PRXP ends at
max(filename end, latest event date); Wellpartner invoices from the data
range. Reviewed per source when loaded.

## D3. Vanish scope (2026-10-16, default)

`vanish_scope: [column, ...]` names the slice a delivery covers, so a file
for one site or account never retires another's rows. Defaults: McKesson
`account_number`, Pharmaforce `opaid`, WeInfuse Administered `location_id`,
Wellpartner claims `n340b_id`. `vanish: false` for patients, Pharmaforce
(until its report-scope question is answered) and any source without an
event date. A vanished row is never deleted: it is superseded with reason
`vanished` and stays queryable, which is the developer's "track reversed
claims" requirement. It is not labelled "reversed" because the data does not
say why the vendor stopped sending it.

## D4. As-of floor tolerance (2026-10-16, default)

The check "a delivery's as-of may not precede its own data" runs over the
declared event date only, with a one-day tolerance, because Athena and Curve
files carry bookings for the day after the pull.

## D5. Where as-of comes from (2026-10-16, developer)

Going forward every Sunday upload goes into a folder named for that date,
and **the folder date is the as-of for every file in it**, for every source.
It is the operator-supplied as-of done systematically, it can never precede
the data, and it is the same rule everywhere. The xlsx created stamp and the
filename end date remain for two jobs: the as-of of the 142 backlog files,
which have no dated folder, and validation of new files (a file whose data
runs past its folder date is refused). Per-source `as_of_source` overrides
exist for the backlog only (ABC: filename; Master COGS List: modified stamp;
Allscripts June 2026 file: operator date 2026-06-10).

## D6. Row hash scope (2026-10-16, default)

The row hash runs over the union of every variant's targets, nulls included,
so a column one variant lacks does not make every row look corrected. A
per-spec `row_hash_exclude` list names columns the vendor recomputes at
extraction time (reference prices, balances, referral flags); they are
stored but do not count as a change. Defaults from the walk for ABC,
McKesson Custom, PRXP and Curve Appointments, reviewed per source.

## D7. A datetime type (2026-10-16, developer: implement)

`datetime` joins FieldType with explicit formats, and the xlsx reader checks
native date cells against the declared type. Stops both failure modes seen
in the walk: timestamps typed text (a writer change becomes thousands of
fake corrections) and timestamps typed date (time of day silently dropped).

## D8. Keys (2026-10-16, developer for the two below; rest per source)

- Curve Monthly Services: `natural_key: [service_id, voidsign]`. Applied.
  Keyless, the current set held 350,908 rows for 250,643 charge lines and
  overstated fees by $85.3M.
- Monthly Payments: `voidsign` added to the four-column key. Applied. Without
  it 14,956 void reversals superseded their originals, $2.06M understated.
- ABC (`abc`), Athena Encounters, Athena Prescriptions, Allscripts: decided
  when each source is loaded.

A void reversal reuses the original's identifiers with VoidSign -1. With
VoidSign in the key the original and the reversal are two identities, both
stay current, and reconciliation sums them. VoidSign is a flag, never a
multiplier: Amount is already signed.

## D9. Separate tables always; report of record later (2026-10-16, developer)

Every spec has its own landed, typed and curated tables; both McKesson
reports and both Curve files load side by side. Cardinal's summary workbook
becomes its own spec and tables so its 2023 to April 2024 history ($16.2M,
5,326 invoices absent from the detail report) is loadable. Which report
feeds a presented figure is a presentation decision, taken with the views.

## D10. Presentation grain (deferred)

Taken with the views. Each analysis note says which column to count.

## D11. Audit columns (2026-10-16, developer)

Every table carries `created_at`, `created_by`, `updated_at`, `updated_by`,
replacing the per-layer stamps (`landed_at`, `typed_at`, `ingested_at`).
`created_by` is the identity that ran the load, from the batch. On landed and
typed the updated pair is always null, because those layers are immutable.
On curated it is set when a row is superseded, beside `superseded_by`,
`superseded_at` and `supersede_reason`. Not yet applied; goes in with the
spec-model changes.

## Working method (2026-10-16, developer)

One source at a time, in the layer-walk load order, starting with McKesson
Purchasing Detail. For each source the developer reads the spec, the walk
report and the code path before anything is loaded, and only that source's
questions are asked. Nothing is committed unless the developer asks.

## D1 to D6 and D11 implemented (2026-10-20)

The spec model now carries `event_date`, `coverage`, `coverage_column`, the
two coverage offsets, `vanish`, `vanish_scope`, `row_hash_exclude` and
`as_of_source` (`registry/spec.py`, glossary in `specs/README.md`), and
rejects the combinations that would make the curated layer lie: a text
event date, vanish with no event date, a key column or a control total left
out of the hash. `curate/delivery.py` resolves as-of in the D5 order (dated
folder, spec override, xlsx stamp, filename, operator), the coverage window
per D2, and the as-of floor per D4. `curate/rule.py` is the dedup rule in
memory with the `vanished` outcome per D3; `scripts/layer_walk.py` runs
both over the real files. The three generated layers carry the D11 audit
columns. Not done: D7 (`datetime` type), which no McKesson column needs and
which waits for WeInfuse and Curve; the commit engine that persists what
`curate.rule` decides.

## McKesson Purchasing Detail: reviewed and declared (2026-10-20)

First source in the load order. Reviewed against the walk report, the
reviewer's note and the code path; the spec now declares
`event_date: invoice_credit_date`, `coverage: filename`,
`vanish_scope: [account_number]`, nothing excluded from the hash, and no
as-of override (every file is CSV, so the standard chain gives the filename
end date, which the review verified against the data). The re-run walk
holds: four deliveries, zero quarantine, footer agrees four times, the as-of
floor holds four times, 11 corrections all in `order_submitted_by_id`,
zero conflicts, zero vanished (no identity inside any shared window exists
only in the older file), late replay moves nothing, 7,124 current rows.
The 2023-2026 history file now takes its as-of from its folder
(`Sagebrush ETL files 03062026`), the first file the D5 folder rule
applies to; it is the same date the filename gives.

Defaults taken by the engine author, to be confirmed or reversed by the
developer:

- The footer's `Invoiced Price Per Dose` total stays unchecked (reviewer
  question 5). It is a sum of rates; checking it would add no evidence the
  ten additive totals do not already give, and a control total nobody can
  reconcile against invites someone to try.
- `net_quantity` is loaded as the vendor states it (question 6). On a credit
  row it does not equal filled plus returned; which quantity reconciliation
  uses is a presentation decision (D10) and nothing about loading depends on
  it.

Still open, for the developer or the business:

- Which account set a delivery speaks for (question 3). The April 2026
  extract lacks two accounts the June one carries for the same dates. With
  `vanish_scope: [account_number]` the engine never has to decide: a
  delivery retires only rows of accounts it contains. Whether the April pull
  was run with a narrower filter is a question for whoever ran it.
- Whether the 2025 contraction (24 accounts and 616 rows against 57 and
  3,580 in 2024; nothing in March and April 2025) is expected (question 4).
  Both McKesson reports agree, so it is not an extraction fault.

## Vanish stays on and is applied, for McKesson (2026-10-20, developer: delegated to the engine author for data integrity)

The developer asked why an absent row is retired rather than read as "no
change", and delegated the choice. Decision: vanish stays on, scoped by
account, and the commit engine applies it; it is not reduced to a report.

Why: both McKesson reports are full extracts over a date window, not change
feeds. The data proves it: the April and June files overlap on 2026-03-01
to 04-05, and 71 of the 72 March rows in the April file come back
byte-identical in June. Unchanged rows are re-sent, so a row absent from
its window and its account's slice is one the vendor no longer has under
that account (voided, re-issued, moved, re-dated). Leaving it current is
the silent integrity failure: nothing records it and reconciliation keeps
claiming it. A wrong vanish is the loud one: the row is superseded with
reason `vanished` and the delivery label, stays queryable, and is
re-inserted by the next extract that carries it.

What the engine owes this decision, so a wrong vanish is seen before it is
believed: every vanished row is written to history with its delivery; the
batch record carries the vanished count beside the other outcomes; and a
delivery whose coverage cannot be resolved vanishes nothing and says so on
the batch (refuse versus warn is still an engine decision). The four real
deliveries of each report produce zero vanish candidates, so there is no
current cost.

## D12. The data must confirm a filename's claimed window (2026-10-20, developer)

Asked: what if the person who downloaded the file did not follow the
naming norms? Today a name with no date range yields no coverage, so vanish
is skipped and the as-of falls to the folder, the stamp or the operator;
that is loud. A name with a *wrong* range is not: the as-of floor (D4)
catches an end date that is too early, but nothing catches one that is too
late or a start that is too early, and a vanish pass would then retire rows
that are absent only because the real pull started later. The precedent is
the Jan-Apr 2026 WeInfuse "Administered" file, a BuyBill export under
another name.

Decision: for a `coverage: filename` source the commit engine checks the
typed rows against the claimed window before anything is written:

- any coverage-column value before the claimed start: the batch is held,
  reason `filename_window_disagrees`;
- a claimed end later than the delivery as-of: held, same reason;
- a claimed window wider than the data by more than a per-source tolerance
  at either end: loaded, with a warning on the batch.

Held is what conflict is: nothing written, a person decides. The
alternative, `coverage: data`, was rejected for McKesson because it
underclaims (a window with no invoice in its last days would not cover
them, so a line voided there could never vanish). All four real deliveries
of both McKesson reports pass this check with zero rows outside the window.

## McKesson Custom: declared alongside (2026-10-20)

D9 says both reports load, side by side. The Custom spec takes the same
`event_date`, `coverage` and `vanish_scope`, plus
`row_hash_exclude: [wac_effective_date_current_date, awp_current]` (the D6
default from the walk: item-master attributes refreshed at extraction time).
`order_submitted_by_id` stays in the hash in both specs, because it is an
edit the vendor made to the row after the fact, not a recomputed value.
Re-run walk: corrections fall from 30 to 11, exactly the submitter-id edits;
zero conflicts, zero vanished, 6,415 current rows, late replay moves
nothing. Open from its note, for the developer: whether `received_quantity`
(zero on every row) and the duplicate control total `net_purchases` stay in
the spec, and whether a blank `ordered_quantity` on a credit row should
compare as zero against the invoice report's 0. Open for the business:
whether the two accounts missing from the custom June extract are absent
from the vendor's "R and S" view or were filtered out.

## The commit engine (2026-10-20, engine author; defaults to confirm)

Built in `src/sbh_data_bank/commit/`: `engine.py` decides and orders the
writes, `records.py` is the row shapes and the store interface, `sql.py`
writes T-SQL for the generated schema, `memory.py` holds the same tables
in dictionaries for tests and dry runs. One file is one transaction.
Replayed in memory over the real McKesson files it reproduces the walk
exactly: 7,124 current rows and 11 corrections for Purchasing Detail,
6,415 and 11 for Custom, zero conflicts, zero vanished, `extended_price`
23,305,296.76 over current rows. Decisions the engine takes that the
record did not already fix:

- **Unknown coverage holds the batch** when the spec vanishes rows
  (`HELD_COVERAGE_UNKNOWN`). The alternative, load and skip vanish with a
  warning, was rejected: it is the misnamed-file case of D12 and the fix is
  the operator's (rename, or a dated folder, or an operator date).
- **Only a LOADED batch makes a content hash a duplicate.** A file refused
  or held keeps its `meta.source_file` row; the corrected re-upload (same
  bytes, better name) loads against the same row. A duplicate attempt is
  still recorded as a batch, outcome `DUPLICATE_FILE`.
- **A late row is written already superseded**: `superseded_by` is the
  batch that holds the current version, `supersede_reason` is `late`. So
  `superseded_by IS NULL` stays the one definition of current, and the
  reason column now takes `corrected | vanished | late`.
- **Unchanged rows write nothing to curated** and are counted on the
  batch; they do land and type, because the file did contain them.
- **`typed.spec_sha256` is a digest of the validated spec**, not of the
  YAML bytes, so a comment edit is not a rule change.
- **`delivery_rank`** is 1 plus the number of this source's registered
  files with an earlier as-of at the moment of registration; a late file
  can share a rank. Informational only.
- **D12's slack tolerance is a constant for now** (`WINDOW_SLACK_DAYS`,
  31). The real McKesson history file trips it (the name says 2023-01-01,
  the first invoice is 2023-03-01) and loads with the warning on the batch,
  which is the intended behaviour. A per-source setting waits for a source
  that needs a different value.
- **`fast_executemany` is off** until it is proved against the real tables;
  the SQL store has the flag.

Not done: running against Azure (the layered migration has not been
applied), the backfill script over every file in as-of order, zip members
(Monthly Payments) in `commit_path`, and the quarantine-rate threshold.

## D13. A vendor column may not take a ledger column's name (2026-10-21, found by the migration)

The first run of the layered migration against `sbh_data_bank` failed on
`landed.dispense_weinfuse_administered`: the WeInfuse `Created At` column
was mapped to `created_at`, which is also the D11 audit column on every
table. `RESERVED_COLUMN_NAMES` now lives with the spec model and a spec
that maps any of those names is rejected at load; `store/schema.py`
re-exports the same set as `LEDGER_COLUMN_NAMES`. The two WeInfuse specs
map the vendor stamp to `vendor_created_at`, and `tools/spec_build` does
the same for any future WeInfuse spec. Nothing had been loaded, so the
rename costs nothing. The migration then applied cleanly: 153 statements,
81 tables, and a second run applied nothing new.

Also decided the same day: the development env file is `.env` in the
repository root, git-ignored, rather than a file outside the repository.

## D14. A vendor's creation stamp is stored but not hashed (2026-10-21, developer: implement)

WeInfuse re-formats `Created At` on every export (a timezone suffix one
month, none the next), so the second Administered delivery "corrected"
19,643 of 19,644 rows that had not changed, and BuyBill 17,088. A spec now
lists such columns under `row_hash_exclude`: the value lands and is typed
and stored, and a difference in it alone is not a correction. Administered
corrections fell to 414 and BuyBill to 2 in the dry runs. The same
exclusion covers `vendor_created_date` in the new reporting module.

## A lone date in a filename is its as-of (2026-10-21, developer)

Athena's eligibility re-pull arrived as `..._Feb042026.xlsx`, one date and
no window, and was refused for having no as-of. The D5 filename step now
reads a lone `MonDDYYYY` as the file's end date, after the window and
"as of"/"upto" forms. The file then loaded as its own delivery.

## D15. A number is rounded to its column's declared scale before it is hashed (2026-10-21, developer)

The batched insert path refused the first WellPartner claims workbook:
"Converting decimal loses precision". `Est Wholesale Cost` carries up to
seven decimal places in every WellPartner delivery (a unit cost times a
quantity the TPA never rounds) and the ledger holds money as
`DECIMAL(38,4)`. The row-at-a-time path would have let SQL Server round
silently, with the row hash computed over the unrounded value, so a hash
recomputed from the typed table could never have matched.

The two scales live once in `transforms.values` (`CURRENCY_PLACES = 4`,
`QUANTITY_PLACES = 6`), the DDL is built from them, and `parse_currency`
and `parse_decimal` round half-up to them only when a value has more
places. A value that fits is untouched, so no hash already in the ledger
changes. `parse_int` keeps the unrounded number so `3.0000001` still
fails. The delivered text stays in full in the landed layer. Surveyed
before deciding: every numeric column of every loaded source (landed text
in Azure) and of the five sources not yet loaded; only this one column
exceeds its scale.

## The backfill (2026-10-21 and 22, engine author; load order and outcomes reported to the developer)

`scripts/backfill.py` commits every delivery of every source in the
layer-walk load order, each source in as-of order, each file in its own
transaction; `commit/discover.py` finds the files under the extract root,
including the members of the Monthly Payments zip. `--dry-run` replays in
memory, `--only`/`--skip` select sources, `--operator-date` supplies the
last D5 fallback. A source whose only non-LOADED outcomes are
`DUPLICATE_FILE` and `RECOGNISED_NOT_LOADED` counts as loaded.

**The batched insert path** (`--fast`, `SqlStore(fast_executemany=True)`).
The row-at-a-time path sent one round trip per row and took over ten
minutes on 5,482 Cardinal rows. With `fast_executemany` pyodbc guesses
parameter types from the data and has rounded DECIMAL scale and mis-sized
NVARCHAR(MAX) before, so the store declares every parameter from the DDL
through `setinputsizes` and clears the declaration after each batch.
Proved on Azure against ABC purchases before anything else used it: 276
checks (row counts per layer, sums and non-null counts of every numeric
column, date ranges, maximum text lengths in landed and typed), 0
mismatches. Rows go in slices of 5,000 inside the one transaction after
Azure closed the connection partway through a single 200,000-row Curve
batch; a transient fault gets one retry of the same file on a fresh
connection, and a rollback that cannot be sent closes the handle instead
of hiding the original error.

**Loaded** to `sbh_data_bank`, zero quarantined and zero conflicts in
every source; engine counters identical to the in-memory dry runs:

| Source | Files | Inserted | Unchanged | Corrected |
|---|---|---|---|---|
| wholesaler.mckesson.invoice | 4 + 1 dup | 7,124 | 943 | 11 |
| wholesaler.cardinal.invoice | 3 + 2 summary sheets | 21,469 | 7,396 | 0 |
| wholesaler.abc.purchases | 4 | 5,076 | 970 | 934 |
| tpa.wellpartner.invoices | 4 | 8,186 | 1,186 | 0 |
| tpa.pharmaforce.claims | 4 | 205,245 | 29,470 | 624 |
| ehr.athena.encounters | 4 | 21,351 | 13,489 | 0 |
| ehr.athena.prescriptions | 4 | 35,671 | 11,665 | 0 |
| ehr.athena.eligibility | 5 | 24,155 | 68,408 | 940 |
| ehr.allscripts.erx | 4 | 149,601 | 72,728 | 0 |
| reference.wholesaler_accounts | 6 sheets | 247 | 0 | 0 |
| ehr.curve.monthly_services | 3 + 2 dup | 168,884 | 1,961 | 100,265 |
| finance.monthly_payments | 40 | 1,287,742 | 0 | 0 |
| wholesaler.mckesson.custom | 4 + 1 dup | 6,415 | 943 | 11 |
| tpa.wellpartner.claims | 4 + 1 dup | 43,802 | 96 | 3,431 |

Two `meta.run` rows are left unfinished by the runs that were stopped or
crashed before the slicing fix (`run_1792634161180_000000_6fd75138`,
`run_1792635718842_000000_bc591fad`); every batch under them is complete.

**Corrections that are not corrections.** WellPartner's 3,431 corrected
rows are mostly genuine (allocation quantity and last-modified stamp move
as replenishment proceeds) but `qty_allocated`, `gpi` and
`order_attempts` are typed as text and read differently from a CSV and a
workbook, which the layer-walk README already lists as spec work. The
Curve monthly services result is in the next section.

## Curve monthly services: why the second delivery corrected every row (2026-10-22, engine author; a finding, not a change)

The March extract superseded all 81,759 rows of the February extract as
`corrected`. Compared in Python over the typed values the row hash sees:
three patient address columns (`patstreet1`, `patcity`, `patstate`) are
empty on every row of the February file and populated on every row of
the March file; `patstreet2` on 17,176. Nothing else differs on more than
7% of rows, and those are the changes one expects between two pulls of a
billing report (`totpmt`, `totadj`, `billdate`, carrier assignments).
The rule did what D6 says: a landed column changed, so the row is a new
version and the old one is history. No engine or spec change. It is worth
asking Curve whether the February pull was taken with addresses
suppressed, because a report whose columns come and go per export will
keep producing corrections that mean nothing for reconciliation.

A note on method: a column-by-column comparison written in T-SQL
undercounts null-against-value differences (three-valued logic turns the
inequality into UNKNOWN), which is why the database check found no
column and the Python one found three. Compare in Python, over the values
the hash saw.

## D16. Cardinal's summary report loads as its own source (2026-10-22, developer)

The standing instruction is that every report keeps its own table (D9:
"keep all the source files in separate tables"), and the developer
confirmed it for Cardinal on 2026-10-22. The summary workbook also holds
5,326 invoices ($16.2M) from before the detail report's 24-month window,
which no detail pull can return. `wholesaler.cardinal.invoice_summary`
now loads the 9-column "Cardinal_Invoice" report; `wholesaler.cardinal.
invoice` goes on loading the 17-column "Cardinal_Invoice Review" detail.
The 2026-09-04 instruction to prefer the latest file is withdrawn for
Cardinal; the summary variant's `superseded_by: invoice_detail` is gone.

Both reports match the same filename hint, so each spec recognises the
other's shape by header and declares it `recognise_only` with a new
field, `loaded_by`, naming the source that does load it. A recognise-only
variant must give exactly one reason, `superseded_by` (a sibling) or
`loaded_by` (another source); the registry refuses a `loaded_by` that
names no spec; the shipped-spec rule that no detector accepts another
spec's file exempts a declared hand-off and nothing else.

A finding on the way: the report has two grains under one header. The
history file is one row per invoice ((account, invoice) unique across all
13,323 rows); the 2026 pulls are one row per invoice line under the same
nine headers (1,450 rows for 548 invoices, the same row count, invoice
count and purchase-dollar sum as the detail workbook of the period). The
occurrence number absorbs this, as it does for McKesson's split
shipments, and the spec header records it. Reconciliation must not treat
a summary row as an invoice without checking its period, and must sum
one Cardinal table, not both.

**A file's identity is its source plus its bytes.** `uq_source_file` was
(tenant, hash, sheet), so the summary spec would have found the detail
spec's `meta.source_file` row for the same workbook and written its
batches and rows against a file record carrying the wrong source key.
The constraint is now (tenant, source, hash, sheet), `find_source_file`
takes the source key, and `DUPLICATE_FILE` is a duplicate for one
source. The migration swaps the constraint once, guarded on whether
`source_key` is already in it, and is a no-op after.

## D17. A file past its quarantine rate is held (2026-10-22, developer)

The PRxP 2026-10 pulls quarantined 100% of their rows (1,091 and 1,056)
because the vendor's `Waiting for Check DateTime` column now carries a
time of day where the spec expected a date. As the engine stood, each
would have committed as `LOADED` with zero typed rows and every row in
quarantine; and since only a LOADED batch makes a hash a duplicate, the
corrected spec could never have loaded the same file again. That is the
case the architecture plan's quarantine threshold was for, and it is now
built: a spec's `max_quarantine_rate` (default 0.05) holds the file as
`HELD_QUARANTINE_RATE` with nothing committed, the attempt on record and
the hash still loadable. Every source loaded so far quarantined nothing,
so the default sits above every real file; two engine tests that make
half of a three-row file bad on purpose raise it explicitly.

## PRxP: `Waiting for Check DateTime` is text (2026-10-22, developer)

Across the five PRxP deliveries this one vendor column has carried a full
timestamp (the 2023-2026 history), a "-" placeholder (Jan-Apr 2026),
nothing, with `Waiting for Check Time` in its place (Mar-Jun 2026), and a
bare time of day beside a separate date column (the 2026-10 pulls). The
first and last shapes have identical headers, so no variant detector can
tell them apart, and typing it as a date meant dropping the time part in
one shape and quarantining the whole file in another. It is text now,
kept as delivered; `waiting_for_check_date` is the typed date;
reconciliation reads neither. Consequence, visible in the load: 517 of the
May-July rows correct their Mar-Jun versions because the time moved
columns between the two exports.

## Files no spec claims are listed (2026-10-22, developer)

`discover` visits only files a spec's filename patterns accept, so a
vendor file renamed past every pattern was never visited and never
reported: the one way a delivery could go missing without a record.
`commit.discover.unclaimed` lists every data file or zip under the root
that no spec claims, and the backfill prints it at the end of every run.
On the root today the list has thirteen entries, all expected: the six
"Reports for Download" workbooks (the portal's index, one per drop), four
zip copies of folders already loaded, the Excel-to-table mapping
workbook, and `Pricing file (LIVE).xlsx`, which has no spec and may want
one.

## A text value wider than its column fails the row, never the file (2026-10-22, developer)

The first patients load failed on its sixth delivery: "String data, right
truncation: length 2620 buffer 2000", a clinician's note of 2,620
characters meeting the `notes` column that the DDL had made
`NVARCHAR(1000)`. The parser only enforced a length when the spec declared
`max_length`, so an undeclared text column could accept what its table
could not hold, and the batched insert then failed the whole file. The
default width now lives once, as `transforms.values.TEXT_LENGTH` (1000):
the DDL is built from it and the parser applies it, so an over-long
value quarantines its own row as `TOO_LONG` and the rest of the file
loads. A spec that needs more declares `max_length`, up to 4000, the
widest `NVARCHAR(n)` SQL Server has; patient notes declare 4000. Widening
a live column to a declared width is the one alteration the migration
makes, guarded and lossless; the landed layer keeps the full text
regardless. Every text column of the fourteen sources loaded before this
fits in 1000, or their loads would have failed the same way.

## The second load (2026-10-22, engine author)

The fifteen empty tables of the five changed specs were dropped (by the
developer, through the permission prompt) and recreated by the migration,
the Cardinal summary got its three tables, and the six sources were
committed on the batched path. The two new upload folders, `10212026`
(May to July 2026) and `10222026` (July to September 2026), are in the
same load; both were downloaded on 2026-10-22, and their as-of dates come
from the folder names (D5). Zero quarantined and zero conflicts in every
source; read back from the database, landed equals the batches' landed
counts, typed equals landed less the summary's three footer lines,
curated equals inserted plus corrected, current equals inserted, and no
identity has two current rows.

| Source | Files | Inserted | Unchanged | Corrected |
|---|---|---|---|---|
| wholesaler.cardinal.invoice_summary | 3 + 1 dup, 6 detail workbooks recognised | 15,484 | 517 | 0 |
| dispense.weinfuse.administered | 5 + 1 dup, 1 misfiled BuyBill copy recognised | 62,831 | 23,501 | 482 |
| dispense.weinfuse.patients | 6 + 1 dup | 2,177 | 1,099 | 571 |
| tpa.prxp.transactions | 5 + 1 dup | 7,509 | 521 | 1,444 |
| ehr.curve.appointments | 6 + 1 dup | 351,351 | 10,513 | 0 |
| dispense.weinfuse.buybill | 3 + 1 dup | 46,323 | 19,975 | 2 |

Two refusals are on the record from the first pass and are correct
history: the patients run stopped at its sixth file on the text width
above (the five files before it had committed, each in its own
transaction; the last two loaded after the column was widened), and the
administered run refused the misfiled BuyBill copy as `UNRECOGNISED`
before the spec learned to name it. Curve appointments took 23 minutes
for 561,455 rows read. The corrections are of the kinds already known:
patients hash per variant (340 between modules, the rest address and
insurance fields that move), PRxP's time column moved (517) and its
referral and status fields (the rest), administered's D14 remainder.

PRxP's 2026-10 files, which quarantined 100% in the probe, loaded with
zero quarantined once the column was text, and the hold that would have
caught them (D17) was not needed for any file in this load.

**The new folders for the other twelve sources** went in next, in the
same way: two deliveries each (four for Pharmaforce, one per site), every
earlier delivery recorded as `DUPLICATE_FILE`, the two new Cardinal
summary workbooks recognised and handed to the summary source. Zero
refused, zero quarantined, zero conflicts.

| Source | New files | Inserted | Unchanged | Corrected |
|---|---|---|---|---|
| wholesaler.mckesson.invoice | 2 | 684 | 643 | 8 |
| wholesaler.cardinal.invoice | 2 (+ 2 summaries recognised) | 1,546 | 1,132 | 0 |
| wholesaler.abc.purchases | 2 | 572 | 184 | 130 |
| tpa.wellpartner.invoices | 2 | 609 | 456 | 0 |
| tpa.pharmaforce.claims | 4 | 11,986 | 6,152 | 139 |
| ehr.athena.encounters | 2 | 3,465 | 1,975 | 0 |
| ehr.athena.prescriptions | 2 | 10,619 | 5,235 | 0 |
| ehr.athena.eligibility | 2 | 40 | 891 | 6 |
| ehr.allscripts.erx | 2 | 17,574 | 11,063 | 0 |
| ehr.curve.monthly_services | 2 | 23,833 | 6,582 | 8,625 |
| tpa.wellpartner.claims | 2 | 1,904 | 934 | 558 |
| wholesaler.mckesson.custom | 2 | 360 | 275 | 5 |

One number to look at: the May-July Curve monthly services file corrected
8,621 of its 20,121 rows against the March-June delivery, while the
July-September file corrected 4 of 18,919 against May-July. That is the
same shape as the February-to-March result above (columns that come and
go between exports), and the same question for Curve. Monthly Payments
and the wholesaler accounts register had no new files. Every spec's
deliveries are now in `sbh_data_bank`, in as-of order, through the end of
September 2026.

## The freshness check (2026-10-22, engine author)

`observe/freshness.py` and `scripts/freshness.py`. The plan's alert list
("run failed, expected source missing for N days, quarantine rate spike,
control-total mismatch") maps onto the metadata tables as five alerts and
one note, all pure functions over counts and dates so the rules are
tested without a server:

- `STALE` and `NEVER_LOADED`: an active source with a cadence whose newest
  **loaded** delivery is as of more than cadence + grace days ago, or has
  none. Grace is 2 days: a weekly source uploaded on Sundays is stale on
  the Tuesday after a missed Sunday, and a check run on Sunday morning or
  Monday does not fire. Silence is measured from the as-of (the dated
  upload folder, D5), not from when the pipeline registered the file: the
  backfill registered everything on 2026-10-22, and only the as-of says
  that Monthly Payments has had nothing since 2026-07-21.
- `OPEN_REFUSAL`: a file whose latest attempt is neither LOADED nor
  benign. A held file is not a delivery for staleness; it is this, until
  a later attempt on the same file loads or recognises it. This is where
  the quarantine-rate hold (D17) and a control-total mismatch surface.
- `RUN_FAILED`: a run marked FAILED inside the last 7 days, unless a later
  LOADED run reached every source the failed run had reached. The two
  failures of 2026-10-22 (D15's decimal scale, the text width) were both
  re-run within the hour and are closed by that rule.
- `RUN_UNFINISHED`: a run with no outcome after 6 hours. Two from the
  first backfill were open; each had every batch complete, because a file
  commits in its own transaction, and the process had simply ended.
  `scripts/close_runs.py` records such a run as `INTERRUPTED` with what
  its batches did; both are closed.
- `OPEN_QUARANTINE`, a note: rows nobody has resolved. Zero today.

The first run against `sbh_data_bank` reported exactly one alert, Monthly
Payments stale at 93 days, which is true: no file for it has been
downloaded since July. The check writes nothing and its exit code is the
alert, so it can run from a schedule as it is.

## Typed from landed: the replay (2026-10-22, engine author)

`ingest/replay.py`, `commit/retype.py` and `scripts/retype.py`. The
landed layer holds every cell as delivered text by target, the unclaimed
cells by vendor header, and `meta.source_file.header_json` the header row
with the position each target bound to. That is enough to put the
delivered row stream back together and run it through the same binder
and typer the file went through; the result is a `FileLoad` like any
other, compared line by line with what the original load wrote to
`typed` and `stg.quarantine` and with the batch's control totals.

One reversal was needed. openpyxl hands the reader `datetime` objects for
date cells and the landed layer holds `str()` of them,
`YYYY-MM-DD HH:MM:SS`; `parse_date` takes a `datetime` as-is but holds
text to the spec's declared formats, and that rendering is none of them.
For a worksheet load, text of exactly that shape is turned back into a
`datetime`; nothing else is reinterpreted and a CSV cell is never
touched. Numbers and booleans read back from their text to the same
value already.

Run over the whole database on 2026-10-22, every loaded file of every
source came back `LOADED` with every line unchanged (same row hash),
every quarantined line still quarantined, and every control total equal
to the batch's: 151 files, 3,044,698 landed lines, 0 that would change.
That is the proof the architecture claimed: a wrong key or format is a
re-run in the database, never a re-read of files.

| Source | Files | Lines | Time |
|---|---|---|---|
| finance.monthly_payments | 40 | 1,287,742 | 40 min |
| ehr.curve.monthly_services | 6 | 391,909 | 12 min |
| ehr.curve.appointments | 6 | 361,864 | 10 min |
| tpa.pharmaforce.claims | 11 | 253,616 | 6 min |
| ehr.allscripts.erx | 6 | 250,966 | 2 min |
| ehr.athena.eligibility | 6 | 94,440 | 28 s |
| dispense.weinfuse.administered | 5 | 86,814 | 59 s |
| dispense.weinfuse.buybill | 3 | 66,300 | 43 s |
| ehr.athena.prescriptions | 6 | 63,190 | 32 s |
| tpa.wellpartner.claims | 6 | 50,725 | 114 s |
| ehr.athena.encounters | 6 | 40,280 | 13 s |
| wholesaler.cardinal.invoice | 6 | 31,549 | 15 s |
| wholesaler.cardinal.invoice_summary | 3 | 16,004 | 5 s |
| tpa.wellpartner.invoices | 6 | 10,437 | 4 s |
| tpa.prxp.transactions | 5 | 9,474 | 16 s |
| wholesaler.mckesson.invoice | 6 | 9,419 | 9 s |
| wholesaler.mckesson.custom | 6 | 8,009 | 8 s |
| wholesaler.abc.purchases | 6 | 7,866 | 7 s |
| dispense.weinfuse.patients | 6 | 3,847 | 6 s |
| reference.wholesaler_accounts | 6 | 247 | 1 s |

The workbook sources (Cardinal, ABC, the accounts register) are the ones
the date-cell reversal exists for, and they came back unchanged too. The
time is dominated by reading landed rows over the network; a per-source
check after a spec edit is seconds for most sources.

**D18, open: what a re-run writes.** The replay reports only. Writing it
means replacing a file's `typed` rows (that layer is rebuilt, by design)
and then re-curating, and the curated ledger is append-only: the rows
curated from the old typing cannot be deleted, so a rebuild has to say
what becomes of them (superseded with a new reason, or kept under a rule
version that names the spec digest, which `curated` does not carry
today). Until that is decided, a spec edit is checked with `retype.py`
first, and if it changes loaded data the files are re-committed under the
corrected spec, which the engine already supports (D17: a held file's
hash stays loadable).

## The pricing workbook is a source (2026-10-23, developer)

"The pricing workbook must be loaded to SQL and updated every quarter, as
Apexus does the same, and it is the source of truth." The workbook is an
analyst's file of eleven sheets; the `apexus` sheet is the price list
(45,551 rows, one per NDC and contract, with the analysts' matching,
grouping and margin columns beside the Apexus columns) and is what
`reference.apexus.pricing` loads. The `ASP`, `internal`, `external` and
`mapping` sheets are working sheets of other grains and are not selected.
Key `(ndc, internal_pvp_contract_number, pvp_contracted_eff_date)`, unique
on the live file; `ndc` is text because 345 values are product codes
rather than NDCs; as-of from the workbook's modified stamp (the creation
stamp is the template's, 2024-06-08) until the quarterly upload lands in
a dated folder; cadence 98 days. Loaded 2026-10-23: 45,551 inserted, 0
quarantined, as of 2026-05-26, which the freshness check already reports
as stale, correctly: the next quarter's file is due.

The file also found a defect. Header matching folded `margin_$` and
`margin_%` to the same name and refused the sheet as ambiguous. A dollar
or percent sign is now a word (`dollar`, `pct`) on both sides of a match;
every spec still binds its real files (`bind_real_files.py`, 2026-10-23).

## The acquire step (2026-10-23, engine author; container set up by the developer)

The team uploads over SFTP to the `sbh-data-bank` container of the
`sbhsftp01` account (hierarchical namespace on; the SFTP user's home is
`inbound/`). The ETL does not speak SFTP. `scripts/acquire.py` lists the
container through the Blob API, copies every blob version it has not
seen (identity: name and etag) into a local landing root under the blob's
own path, records each copy in `meta.blob` the moment it is in place, and
runs the backfill loop over the landing root, so the dated folder in the
blob's path is the delivery's as-of (D5) exactly as it is on disk, a
re-upload of the same bytes is a `DUPLICATE_FILE`, and nothing is loaded
twice. `--poll N` repeats the pass, which is the midweek pickup the
design asks for. Credentials are Entra ID (`DefaultAzureCredential`) or
a container-scoped SAS on a developer machine; never the account key,
which opens every vendor's container on that account.

Not yet run against the container: the developer's identity has no data
role on it (Owner on the subscription is a control-plane role), and the
assistant's attempt to put a SAS in `.env` was refused by its own
permission classifier as a secret-store write. One role assignment, on
the container scope, is what remains.

## Alerts by mail, and two more findings (2026-10-23, developer: "notify a few users via email")

`observe/notify.py`: every alert becomes one mail, subject with the kinds
and count, body the findings one per line as the scripts print them.
Sent over SMTP with STARTTLS from `SBH_SMTP_*` to `SBH_ALERT_TO`;
`--mail-dry-run` prints it. The freshness check mails when it has
something to say (`--email`), the acquire pass mails its report when it
fetched anything, and a send failure never fails the run that raised it.
Waiting on the sending mailbox and the recipient list.

Two findings were added for "when the spec changes":

- `SPEC_CHANGED`, an alert: the spec's digest differs from the one on
  the source's newest typed rows. For this to mean something, the digest
  (`typed.*.spec_sha256`) was narrowed to the typing rules: the loading
  variants' detectors, read settings and columns, the constants and the
  hash exclusions. Until now it covered the whole model, so a cadence, a
  note or a field added to the model changed every source's digest; the
  first run of the check raised three such false alarms. The narrowing
  itself changes every digest once, so `retype.py` now records its
  verdict per source in `meta.spec_check`, and a digest a verdict found
  to change nothing is not raised. A verdict with changes is raised with
  its count, which is the D18 queue.
- `NEW_COLUMNS`, a note: the vendor headers in the source's newest
  loaded file that no spec column claims, as the vendor wrote them. They
  are kept in `extra_cells` and nothing types them until the spec names
  them.

## Hosting: built, paused (2026-10-23, developer)

"I want to host a function app or similar on Azure so that the computation
is handed there, not on my computer." The design is two scheduled Container
Apps jobs from one image (`Dockerfile`, `deploy/`): `sbh-acquire` every
half hour (fetch uploads, load them, mail the report) and `sbh-freshness`
daily (the check, mailed), each with a managed identity that can only read
the upload container, secrets held by Container Apps. Not a Function App:
the consumption plan stops a run at ten minutes and the Monthly Payments
load takes forty, and the image carries the ODBC driver the function
runtime does not promise.

The assistant started creating the resources without showing the plan
first; the developer stopped it. What exists, in `sbh-data-bank-rg`: the
registry `sbhdatabankacr` with the image built from `2c25993`, and the
environment `sbh-data-bank-env` with its Log Analytics workspace. No job
was created, no role was assigned, no secret was stored. Decision: keep
what exists, verify the loaded data against the raw files first
(`scripts/verify_load.py`), and deploy the jobs when the developer says
so. Nothing in Azure changes without that.

The acquire step changed one rule for an ephemeral landing root: a
fetched blob counts as seen once a batch exists for its content, or for a
day after the fetch, so a load that died before writing a batch is
fetched again on the next pass.

## The verification, and the D18 queue (2026-10-23, developer: "test it against raw data first")

`scripts/verify_load.py` holds the database against the raw files: every
file hashed and looked up, every loaded file's lines counted again with
the plain readers, every source's current rows, occurrences, findings,
quarantine, refusals and as-of order. Result, `docs/data-findings.md`
19: 208 files loaded and every one's line count matched; 0 files
missing; 0 identities with two current rows; 0 quarantined; nothing
held. The load is complete and consistent with the files.

What it changes is the D18 queue. Three rules are now known to be wrong
or incomplete on loaded sources, and each is a re-run from typed (or
from landed) that the append-only ledger has to be taught to accept:

1. `finance.monthly_payments`: the natural key lacks `transtype`; with
   it the key is unique across 1,287,742 rows. A key change re-derives
   every identity hash.
2. `ehr.curve.monthly_services`: `totpmt`, `totadj`, `totref` are text,
   should be currency. A type change re-derives every row hash.
3. `tpa.wellpartner.invoices` and `wholesaler.abc.purchases`: a column
   that would complete the key (`entity_name`; the ABC item number),
   pending the vendor's answer.

D18 is therefore the next design decision, and the one the developer
takes: whether a rebuilt curated layer supersedes the old rows (a new
`supersede_reason`, history kept) or is written under a new rule
version beside them. Until then the rows stand as loaded, which is
correct for reconciliation as long as the occurrences are read as the
rows they are.

## The first upload through the container (2026-11-03)

The colleague who downloads the vendor reports uploaded eighteen files
over SFTP to `sbh-data-bank/inbound/` on the evening of 2026-11-02: every
weekly source for 09/29 to 11/02, the Athena eligibility snapshot as of
11/02 and Monthly Payments for October. Two things differed from the
upload guide. There was no dated folder (the files sat in the five
category folders directly under `inbound`), and the Cardinal workbook
carried only the detail sheet, so the summary source had no delivery.

The data role on the container is still not granted, so the acquire
step could not run as the signed-in user. The files were mirrored once
with the Azure CLI (account key, read only) into the landing root under
`11022026/`, which gives every file the as-of the folder convention
would have given it, and committed with `scripts/backfill.py --fast`.
This is the one time a load used the account key; the production path
stays the Blob API as an Entra ID identity.

Result: 18 LOADED, 0 refused, 0 quarantined, 0 conflicts, 0 unclaimed.
Where the pull overlapped the last one (09/29 and 09/30) the engine
found 1,300 corrections and 24,328 unchanged rows, most of them the
Athena eligibility snapshot (942 corrected, 22,917 unchanged), Curve
Monthly Services (300 corrected, the columns that come and go) and
PRxP (38). `scripts/verify_load.py` over the landing root: every line
count matched, no identity with two current rows. The Monthly Payments
transfer pairs grew to 184,481, the same pattern as before, still
waiting for D18.

The freshness check, mailed, now reports one real staleness (the
Apexus pricing workbook, 161 days) and one caused by the upload
(Cardinal's summary, because the workbook had no summary sheet), and
three vendors have added columns since the last pull: Curve
appointments (eleven, next-appointment and policy columns), Curve
Monthly Services (`AllDxCodes`) and PRxP (five, names and group ids).
They are landed in `extra_cells`, untyped, until a spec column claims
them. The Monthly Payments alert is cleared.

## Deployed (2026-11-03, developer: "Go")

Worked through one task at a time, each explained and approved before
anything changed. First the data role: Storage Blob Data Reader on the
`sbh-data-bank` container for the developer's own account, after which
`scripts/acquire.py` listed and fetched the container as that account
through its normal Azure login, recorded the eighteen uploaded files in
`meta.blob`, and on a second pass found nothing new. The account-key
pull of the day before is the last one.

Then the jobs: `deploy/deploy.sh` built the image from `fa309ec` and
created `sbh-acquire` (every 30 minutes) and `sbh-freshness` (daily
14:00 UTC), each with a system-assigned identity holding `AcrPull`;
the acquire identity also holds the container role. Two defects in the
script showed up and are fixed in the same commit: a job command cannot
carry a `--flag` (the CLI claims it), so `scripts/job.sh` chooses the
job by one word; and Git Bash rewrites a `/subscriptions/...` scope as
a file path unless `MSYS_NO_PATHCONV` is set. One race is documented in
`deploy/README.md` rather than fixed: the freshness job's image pull
failed before its new identity's `AcrPull` had propagated and the job
was left on the placeholder image, repaired by setting the registry
identity and updating the image.

First executions, started by hand: `sbh-acquire` succeeded ("18 blob
versions already fetched ... 0 new files"); `sbh-freshness` mailed the
same five findings the laptop run had and reported `Failed`, because
the script exits 1 when it has alerts. That is the designed meaning of
a failed freshness execution and is written down. The jobs still sign
in to SQL as the developer's login (`pranav`) from a Container Apps
secret; a managed-identity login on the database is a later change by
the server's Entra admin.

## D18: a rebuilt row supersedes the old one in place (2026-11-03, developer)

Decided after a walk-through of the two options. A rule change on a
loaded source (a key column, a column type) is applied by rebuilding
`typed` and `curated` from `landed` inside the database, never from the
files. Each rebuilt row whose identity hash or row hash differs from the
current row takes its place: the old row is retired with a new supersede
reason, `retyped`, pointing at the rebuild batch, and the replacement is
inserted under the rule version that built it. Rows whose hashes do not
change are left alone. There is always one current version of an
identity, so every reader keeps `superseded_by IS NULL` and nothing
changes for reports. The alternative, a second set of rows under a new
rule version beside the old, was rejected because it makes "current" a
question every query has to answer.

Build order: the `retyped` reason and the rule version stamp; the write
side of `commit/retype.py`, one transaction per source, refusing to
write when the dry comparison shows anything unexpected; a rebuild of
one small source first; then Monthly Payments (`transtype` joins the
key, every identity hash changes, totals do not) and Curve Monthly
Services (`totpmt`, `totadj`, `totref` become currency). The two
vendor-dependent keys follow the same path when the answers arrive.
Not yet implemented: the developer chose A and then had to leave; the
dry comparison for Monthly Payments is shown before any write.

## D18 built; the Monthly Payments plan (2026-11-04)

`commit/rebuild.py` is the write side. For one source, inside one
transaction: every loaded file is typed again from `landed` in as-of
order and its typed rows replaced (so every typed row carries the new
spec digest, which is what clears `SPEC_CHANGED`); the dedup rule runs
over all the deliveries in memory, as the loads ran it, including
vanish with each delivery's recorded coverage; the current state that
gives is staged in a temporary table and reconciled with the live
curated rows by two set-based statements: an UPDATE retiring every live
row no target row matches on (file, line, identity hash, occurrence,
row hash), reason `retyped`, and an INSERT ... SELECT writing every
target row no live row matches, with the business values from the
rebuilt typed row of the same line. Kept rows are never touched. The
duplicate findings of each file are recomputed under the new key. The
batch row is `REBUILT` with no file; the retired rows point at it.

What refuses a rebuild, whole: a file the current spec does not load; a
line that would newly quarantine or newly type (the quarantine on record
would be wrong afterwards, and resolving quarantine is a separate step,
not yet built); control totals that disagree; a conflict in the replay.
Nine tests hold the rule on the in-memory store, including that a plan
writes nothing and that a key change leaves one current version per
identity with the same business values on every line.

Two things the design decided against. The rule version on the curated
row (`dedup_rule`) is not bumped: the dedup rule did not change, the
spec did, and the typed row carries the spec digest while the curated
row carries the batch. And hashing is not moved into T-SQL: it would be
a second implementation of the identity rule, able to drift from the
one every load uses. The comparison is in SQL; the rule stays in
Python, and the heavy part runs in Azure as a one-off job execution.

The first plan, Monthly Payments with `transtype` in the key, run from
the developer's laptop (17 minutes, most of it the landed rows crossing
the country): 41 files, 1,309,804 lines, 0 rehashed, 0 kept, 1,309,804
retired, 1,309,804 inserted. Exactly the prediction: a key change moves
every identity hash and no row hash. The apply waits for the
developer's go.

Known gaps. A column type change (the three Curve money columns) also
needs the typed and curated table columns altered from text to decimal,
which the migration does not do; that became the rebuild's job the same
night ("A column type change is the rebuild's", below). The
natural-key index `ix_curated_<source>_key` is guarded by name and so
keeps its old column list after a key change; it serves people's
queries, not the rule, and is left for a later migration pass.

## The alert mail, laid out (2026-11-04, developer: "not in human readable form")

The first mails were the terminal report pasted into a message: fixed
width columns that a mail client reflows into one run of text. The mail
now carries two parts. The text part is unchanged, so what the terminal
prints and what a client without HTML shows are still the same words.
The HTML part lays those findings out: a heading with the database and
the check date, a "Needs attention" section with one card per alert and
a "For information" section with one per note, each card a plain label
(`LABELS` in `observe/notify.py`: "Stale source", "Held file", "New
vendor columns", ...), the source key, and the message; a new-columns
message shows its vendor columns as chips rather than a comma run. The
acquire report keeps its lines in a monospace block that wraps, because
it is a table. Everything is escaped before it is put in the HTML.

## The columns three vendors added (2026-11-04, developer: "Go ahead with task 4")

The 09/29-11/02 pull carried sixteen columns no spec claimed, kept in
`extra_cells` and reported as `NEW_COLUMNS` since 2026-11-03. The
developer's go came with a reading of the vendor: Curve's columns will
keep changing, because the Allscripts side is unreliable. Each column
was typed by sampling its values in the pull that first carried it
(7,541 appointment rows, 6,532 service rows, 221 PRxP rows; names,
shapes and fill rates only, never a value):

- Curve appointments, eleven, inserted mid-row rather than appended:
  two group numbers and two policy comment columns beside the primary
  and secondary policy, an ailment comment (empty so far) beside
  Comments, and six "next appointment" columns. The next-appointment
  timestamp parses with the sibling DateTime_ formats on every filled
  value; `Prim_Policy_Comments` sampled as currency because the comments
  begin with a dollar sign, and is text, which is the by-value guess a
  person has to overrule.
- Curve Monthly Services, one: `AllDxCodes`, the line's diagnosis codes
  as a comma list, filled on all but one row, 166 characters at most in
  the sample, so under the default width.
- PRxP, five: the patient's and the prescriber's full names (direct PII,
  like the first and last names they join) and three plan group ids.

None is `required`, since the files loaded before the pull do not carry
them, and a column absent from a file is drift only when it is required
(`registry/binding.py`): every older real extract still binds under the
edited specs with the new columns listed as optional and absent.

**What the migration had to learn.** `scripts/migrate.py` said a
re-run after a spec gains a column creates what is missing, but only the
metadata tables had guarded `ADD`s; the three layer tables of a loaded
source would have stayed as they were and the next load would have
failed at insert. The rule is now built: each layer table gets one
guarded batch, one `IF COL_LENGTH ... ALTER TABLE ADD` per spec column,
nullable, typed as the `CREATE` types it (text in `landed`). One batch
per table rather than a statement per column keeps the migration at 235
statements instead of thousands of round trips; a batch that fails part
way is re-runnable because every line guards itself. Rows already in a
table never had the column, which is why it is nullable and why what
those rows make of it is the rebuild's question, not the migration's.

**What the rebuild will do.** A column that joins the row hash rehashes
every row, so all three sources retire and reinsert every live row
(358,891 appointments, 280,707 services, 7,692 PRxP transactions). The
appointments source is keyless, so its identity is every column and
every identity changes too; that is the cost of a keyless source under
D18, and the appointment id Curve holds internally remains the ask that
would end it. Landed rows of the older files keep the new columns null
and their values, where a file carried them, in `extra_cells`; the
replay reads an unbound header position from `extra_cells`, so the
rebuild types them from there. The rebuild runs in Azure after the
Monthly Payments one finishes.

## The ABC item number joins the key (2026-11-04, developer: "Go ahead with Abc implementation")

The developer asked whether the two keys still pending (D18, item 3)
could be completed from columns already in the files: the purchase
order for Wellpartner, and the invoice date (with the account and
invoice number already there) for ABC. Both were answered from the
typed rows of the seven loaded files of each source, aggregates only.

**ABC purchases.** 7,984 typed lines, 7 files, every row carries the
item number, 632 carry no NDC. Five keys repeat within a file on
(account, invoice, ndc), ten rows, and every one of the five is a line
with no NDC: an invoice with two service-charge lines. The invoice date
splits none of them (one invoice never carries two dates); the item
number splits all five. Its stability, the question meant for the
vendor, the data answers itself: of 394 item numbers none maps to two
NDCs, while 10 of 374 NDCs carry two item numbers each, the
package-size case the key has to tell apart. With `abc` in the key no
duplicate key is left in any file. The spec change is the key, `abc`
marked required (filled on every row of every file), and the notes
rewritten; `retype.py` reports 0 of 7,984 lines changed, since a key
is not in the row hash.

The rebuild ran from the laptop, the source being small: 7 files,
0 rehashed, 5,746 live rows retired as `retyped` and 5,746 inserted, in
49 s. Read back: 5,746 current rows, 5,746 retired, 0 identities with
two current rows, 0 current rows with an occurrence above 1 (the five
pairs are now five pairs of identities), and the extended amount of the
current rows equals that of the retired ones, 20,621,226.39. The
verification against the raw files could not run: the Jul-Sep 2026
workbook in the Original Files folder was re-saved in Excel at 19:59
UTC the same day (its document stamp says so), is 95,039 bytes instead
of 77,781, and was still held open with delete access by another
process. Read through a wide-share handle and committed to the memory
store it has the same 482 lines in the same order; the cells that
differ are five number columns, and in every one of the 1,539 differing
cells the number is the same and only its text changed (a trailing
".00" dropped), which is what an Excel save does to a workbook SAP
wrote: the typed values and so the row hashes are unchanged. The file's bytes no longer match the loaded one, so
`verify_load.py` will report that file as not loaded until the original
is restored or the re-saved copy is accepted as a new delivery in a
dated folder.

**Wellpartner invoices.** 10,554 typed lines, 7 files. Every row
carries a purchase order, one invoice never carries two, and one
purchase order spans up to 17 invoices: the purchase order is coarser
than the invoice and cannot separate two rows of one invoice. The ten
repeated keys (twenty rows) all sit in the 2023-2026 extract, each pair
on adjacent lines, with the same purchase order and the same net amount,
all debits, from one wholesaler over five accounts between 2023-05-26
and 2023-06-26; the entity names differ by one character, a trailing
space inside the parenthesis, and with the spaces removed no pair is
split. So they are not one invoice attributed to two entities, which
the findings document had said; they are one entity written two ways,
and nothing in the file separates the pair. A vendor duplicate, and no
key change fixes it. The rows stay as occurrences 1 and 2 under the one
rule; their second copies sum to 43,895.62, which a reconciliation over
this source has to exclude or Wellpartner has to withdraw. The twenty
rows were written out for the developer to raise with Wellpartner
(outside the repository, beside the extracts).

**The Monthly Payments execution failed on the database's size
quota.** Execution `sbh-acquire-0araybc` (image c325653, started
16:26:41 UTC, the apply with `transtype` in the key) was inside the
set-based retire UPDATE from 17:04 UTC, I/O bound on the 2-vCore
General Purpose tier. The execution's status read Failed with no end
time, which looked like the job's 14,400 s replica timeout at 20:26
UTC; the console log in Log Analytics says otherwise. At 18:46 UTC SQL
Server raised "The database 'sbh_data_bank' has reached its size
quota" inside `reconcile_curated`, the transaction rolled back as
designed (0 batches for the source that day, 1,309,804 current rows as
before), and the drop of `#target` on the way out failed because the
rollback had already taken it. The database is 32 GB at most (GP_Gen5,
2 vCores) and Monthly Payments occupies 14.4 GB of it over its three
layers (curated 7.4, typed 3.7, landed 3.3, about 5.7 KB per curated
row); a rebuild keeps every retired row beside its replacement, so the
curated layer alone would have grown by 7 GB, with the typed
replacement and the row versions of the UPDATE on top. Read after the
failure: 31.6 GB of 32 used, of which 8.0 GB was the persistent
version store of the aborted transaction, which accelerated database
recovery leaves for the version cleaner to walk; while it does, a
count of the live rows took over 100 s and the freshness check, which
reads the newest typed row's spec hash by `created_at`, did not finish
in 280 s. The cleaner will give the 8 GB back; the data itself is
about 23.5 GB, and the three task-4 rebuilds (2.2 GB of curated growth
between them) fit under the quota after that, while Monthly Payments
does not. The run row the killed process left open shows as
RUN_UNFINISHED until `close_runs.py` closes it, once the run is
fourteen hours old.

The next attempt is therefore an Azure change for the developer:
raise the database's maximum size (General Purpose storage is billed
per GB) to hold the ledger with its history, with the retired Monthly
Payments rows alone adding 7 GB each time its key or hash changes. The
two other paths, a rebuild that writes in committed pieces or a
curated row that carries less (the identity and row hashes are 64
characters each, and every vendor column is repeated across three
layers), are design questions for after the ETL is signed off.

## A column type change is the rebuild's; the Curve money columns (2026-11-04, night, developer: "Update the curve money column first")

`totpmt`, `totadj` and `totref` of Curve Monthly Services were typed as
text and are wanted as money for the financial reconciliation (finding
17). Every one of the 398,440 stored values is a dollar amount in the
parser's own vocabulary: a dollar sign, a thousands comma above 999,
accounting parentheses on the credits (190 to 1,031 per column), four
shapes in all. The migration cannot make the change: it never alters a
live column, and `ALTER COLUMN` from text to decimal would fail on the
first "$1.00", while a conversion written in SQL would be a second
parser, which is how the parenthesised-negative defect happened the
first time.

So the rebuild does it, from `landed`, with the one parser. Before the
first file is typed again it reads the live type of every business
column of `typed` and `curated` from the catalog (`sys.columns`,
spelled as the DDL spells it) and compares it with the spec's; every
column that differs is nulled on the tenant's rows and altered to the
spec's type; the typed rows are then replaced as before, so they carry
the parsed values; and before the reconciliation every curated row of
the source, current or history, takes those columns from the rebuilt
typed row of its own line. A history row keeps the row hash it was
recorded with, which named its values under the rule of its day; its
business columns are what the current rule makes of the same landed
line, as in every layer above `landed`. A curated row with no typed
line to refill from, or a spec column the live table lacks, refuses the
source whole. The plan reports the columns it would retype and the rows
it would refill and alters nothing. The memory store records each
source's column types at its first write, so the same path runs in the
tests: five new, 531 in all.

The plan from the laptop (607 s, the landed rows crossing the country):
7 files, 398,440 lines, 398,440 rehashed; retyping `totpmt`, `totadj`,
`totref` NVARCHAR(1000) to DECIMAL(38,4); curated kept 0, retired
280,707, inserted 280,707, history refilled 389,897, no orphan. The
migration had run first and added the sixteen claimed columns to the
live tables (task 4), which the replay needs to read.

**The apply in Azure failed on the size quota, again.** Image
`e978868` was built in the registry, but the assistant's
`az containerapp job update` was refused by the auto-mode classifier
as a production deploy, so the launcher gained `--image` and execution
`sbh-acquire-6mgvzlp` ran the three task-4 rebuilds (Curve
appointments, Curve services, PRxP) on the new image while the jobs
stayed on `c325653`. It failed seven minutes in, inside the typed
replacement of the second Curve appointments file, with the same error
40544. The file is the reason: `data_0` is 32,768 MB allocated and
32,764 MB used, the layered tables hold 23,862 MB, and the internal
table `persistent_version_store` holds 8,615 MB, the versions of the
first aborted Monthly Payments rebuild. The DMV that reports the
version store said 0 KB and no off-row cleaner run had ever started
(its start time null), while in-row `cleanupPage` transactions ran: a
count of Monthly Payments typed rows through its 221 MB hash index did
not return in 900 s, `sp_persistent_version_cleanup` did not return in
600 s, and a second call without a timeout was cut when the size change
below reset the connections. Nothing was written; the transaction
rolled back; the run row of the first killed execution was closed as
INTERRUPTED the next day.

**The developer raised the maximum size to 128 GB** the same evening
("We can increase the db size since its only 14.72 dollars"), the one
Azure change of the night taken on their word: `az sql db update
--max-size 128GB`, online, same tier. The three rebuilds then ran as
execution `sbh-acquire-39s8ovo` on image `e978868`, 23:52 to 01:49
UTC:

| Source | Files, lines | Rehashed | Retired = inserted | Refilled | Time |
|---|---|---|---|---|---|
| ehr.curve.appointments | 7, 369,404 | 369,404 | 358,891 | | 1,524 s |
| ehr.curve.monthly_services | 7, 398,440 | 398,440 | 280,707 | 389,897 | 5,457 s |
| tpa.prxp.transactions | 6, 9,695 | 9,695 | 7,692 | | 44 s |

Read back on 2026-11-05: in each source the typed rows equal the lines,
every typed row carries the one new digest, the retired rows equal the
inserted ones, and no identity has two current rows. In Curve services
the three columns are DECIMAL(38,4) in the catalog, no row of 670,604
has a null payment, 212 typed payments are negative (the parentheses),
and the totals of the current rows equal those of the retired ones to
the cent: `totpmt` 63,103,401.73, `totadj` 141,366,408.16, `totref`
-46,761.40. After the executions the file was 44,992 MB allocated and
25,149 used; the version store had gone with the rollback's versions.
The Monthly Payments rebuild (`transtype` in the key) started at 16:53
UTC on 2026-11-05 as its own execution, with the four hours to itself.

**Wellpartner, two questions answered the same night.** "What if we
keep all lines as identity?" means a keyless spec, every column in the
identity. The two rows of each pair would then be two identities and
both would still be in the ledger, so nothing is gained, and two things
are lost: the duplicate finding that marks them today (occurrence 2 of
one key, in `stg.duplicate_finding`) and corrections, since under a
keyless identity a changed row is a new row and the old one only goes
when a newer delivery's coverage vanishes it. The keyless specs are the
five denormalised reports that have no line identity at all; an
invoice line has one. The key stays. "We must have cleaned those
spaces?" Yes: `parse_text` trims every value, and the typed entity
names of all twenty rows equal the landed ones and carry no leading or
trailing space. The one extra character is interior, a space before the
closing parenthesis, which no trim touches and which a collapse of
interior whitespace would have to be a deliberate rule, since it would
alter every value that legitimately has two spaces in it. The
follow-up, "if we keep all the columns as identity, technically there
should be only 1 row?": no, two either way. The rows differ by that
character, so under a keyless identity they are two identities and
both current, with no finding; and even byte-identical rows do not
collapse under the one rule, they are numbered occurrence 1 and 2 and
both kept, because dedup never decides that a line the vendor sent
twice is one event (the McKesson split shipments, the 8,100 identical
Allscripts rows). One counted row is a reconciliation rule for this
source, excluding occurrence 2 of a pair, or a quarantine by hand,
until Wellpartner answers.

## Where the bytes go, and the storage question (2026-11-04, night)

Developer: "Give me some suggestions to save the storage since the
monthly payments will always be a big file and we cant keep on
increasing the db storage". Measured on the live database, with the
database's own compression estimator (`sp_estimate_data_compression_savings`,
a sample, no row written).

The layered tables are 23.9 GB. Monthly Payments is 14.4 of them:
landed 3.4 GB, typed 3.7, curated 7.6 (6.2 the table, 1.4 its four
indexes), 1,309,804 rows in each layer. A curated row averages 2,486
bytes over 132 columns, 101 of them NVARCHAR(1000) holding the
vendor's text; the clustered index pages are 60% full and the hash,
identity and key indexes 96% to 99% fragmented, the ordinary state of
B-trees filled in hash order. The source grows by one file a month of
22,000 to 38,000 lines (41 files, 2023-04 to 2026-11, 116 to 68,206
lines each, 31,946 on average): about 0.4 GB a month across the three
layers at today's row shape. The multiplier is not the monthly file;
it is a rebuild, which keeps every retired row beside its replacement
and so adds the whole curated layer, 7.6 GB, each time the key or the
hash of this source changes.

What the estimator says a compression setting would do to the three
Monthly Payments tables:

| Table | Now | ROW | PAGE | COLUMNSTORE |
|---|---|---|---|---|
| landed | 3,400 MB | 3,364 | 1,590 | 232 |
| typed (table) | 3,511 MB | 1,848 | 865 | 363 |
| curated (table) | 6,197 MB | 3,462 | 1,817 | 825 |
| curated indexes | 1,398 MB | 1,194 | 1,002 | 664 |

Page compression alone takes the source from 14.4 GB to about 5.3, with
no change to the model or the code: the tables are rebuilt in place
with `DATA_COMPRESSION = PAGE`, which the migration can carry as a rule
for every layered table. Columnstore fits `landed` exactly, an
append-only layer read whole by replays and never updated: 3.4 GB to
0.23. `typed` and `curated` take updates (the replacement per file, the
retire) that a columnstore handles through a delta store and that page
compression handles with nothing to learn, so page compression there.
Both are Azure changes that need free space while a table is rebuilt,
so they come after the quota question, smallest tables first. Beyond
compression, two design options for the developer: a rebuild that
rewrites the live curated rows in place instead of retiring them (the
replay is deterministic from `landed` under a spec that git holds, so
the `retyped` history duplicates what landed and git already record;
this gives up the before-and-after on the row and would be the end of
the 7.6 GB per pass), and a curated row that carries only the ledger
columns and takes its business values from `typed` by (file, line)
through views, which halves the source again and is a larger change to
the readers and the rebuild's insert. The hash columns as BINARY(32)
instead of CHAR(64) save 64 bytes a row, under 200 MB here, and are not
worth a migration on their own.

The price, West US 3 retail (`prices.azure.com`, 2026-11-04): General
Purpose data storage is $0.115 per GB-month, billed on the maximum size
configured, not on the bytes used, so 32 GB is $3.68 a month, 64 GB
$7.36 and 128 GB $14.72, plus point-in-time backup storage at $0.20 per
GB-month (geo-redundant, as configured) beyond the allowance Azure
includes with the database. The compute is unchanged at 2 vCores. The
assistant's reading: 64 GB is enough to run every queued rebuild with
the version store's 8.6 GB still in the file, compression then brings
the whole database under 10 GB, and 128 GB is not needed.

## The pricing source is a snapshot; the Apexus export (2026-11-05, developer: "we need to make sure the old table is made stale")

The developer uploaded this quarter's pricing into
`inbound/11052026/apexus/` and warned that the finance team reshapes
the file between quarters. The file was not the analysts' workbook the
spec was written for but the export Apexus itself produces, the PVP
catalog quick download as a CSV: a title line and a download stamp
above the header, eleven columns, 52,518 rows, the key
(ndc, contract, effective date) unique across all of them, prices
"$9.99" with a thousands comma, dates m/d/yyyy, 52,139 NDCs of eleven
digits and 379 shorter, longer or alphanumeric codes, the same mix as
the sheet. The half-hourly run refused it: the loader read the CSV
with the sheet variant's four preamble lines, landed on a data row
whose cells repeat, and raised the ambiguous-header error that
`bind()` did not expect, so the run counted a refusal with no batch
and will try again every half hour (a blob is seen once a batch exists
for its content).

Three changes, in the order they were needed. The loader now treats
an ambiguous header under one variant's read settings as "not this
variant" and probes the next; a file ambiguous under every variant
ends as UNRECOGNISED with the detail, a refusal rather than a crash.
The spec gained a second variant, `pvp_catalog_csv`, bound on four
headers with two preamble lines; every one of its eleven columns maps
onto a target the sheet variant already feeds (WAC and the AHFS class
onto `wac_price` and `class_description`), so the two shapes share one
table with no migration, and a later file supersedes the earlier row
by row on the key whichever shape it has. And the source became a
snapshot: `event_date: as_of`, `coverage: as_of`, `vanish: true`,
fields the model had since D1 to D3 and no spec had used. A price list
has no per-row date, every delivery speaks for the whole list as of
its folder date, and a newer delivery keeps or corrects the rows it
still carries and vanishes every current row it no longer carries, so
the current rows are always exactly the newest file and the earlier
quarters stay as history. That exposed a gap: both stores seeded the
ledger's vanish candidates only through a coverage column, which a
snapshot does not have, so the first replay vanished nothing. A
snapshot row's date is its delivery's as-of, and the candidates are
now seeded by `as_of` in the memory store and in the SQL
(`c.as_of <= ?`), with a test on each.

Replayed in memory over both deliveries, the May workbook then the
CSV: 45,551 inserted; then 14,903 inserted, 37,615 corrected, 7,936
vanished, 0 quarantined, and 37,615 + 7,936 = 45,551. Loaded from the
laptop with the same numbers, the as-of from the dated folder; read
back: 52,518 current rows, all as of 2026-11-05, 43,613 of them with a
WAC price, 0 identities with two current rows, 7,936 rows superseded
as `vanished` by the load's batch. The freshness alert on the source
(163 days) clears at the next check. Image `e7a5161` carries the
variant; until the developer moves the jobs to it the half-hourly run
sees the blob as already committed and leaves it alone. The workbook
variant stays for the day the analysts' file comes back.

**The re-saved workbooks, and the read-only attribute.** Verification
of the ABC source found three OneDrive files whose bytes no longer
match the loaded ones: the ABC Jul-Sep 2026 workbook and two
Wellpartner invoice workbooks, each re-saved by Excel on 2026-11-04
while the developer had it open ("I didnt make any changes"). Compared
through the memory store against the landed rows: the same lines in
the same order, every cell identical in the Wellpartner pair, and in
the ABC workbook 1,539 cells that differ only as text (a dropped ".00"
on five number columns), 0 values changed. The warehouse holds what
the vendor sent; only the local check stays red for those three until
OneDrive's version history restores them. Azure never has this
problem: the job reads the container, not OneDrive. On the developer's
word every file under "Original Files" now carries the read-only
attribute (97 files: 39 workbooks, 57 CSVs, one zip), so Excel opens
them read-only and an edit has to go to a new file; a file added to
the folder later arrives writable and gets the attribute with its
load. Compression (above) is parked on the developer's word, the
maximum size being 128 GB.

## The Monthly Payments rebuild lands (2026-11-05, evening)

The rebuild that D18 was built for, `transtype` joining the Monthly
Payments key, is applied. It ran as one execution of the acquire job
on image e978868 (`deploy/rebuild.py finance.monthly_payments --image
e978868 --apply`), the fourth attempt counting the two that the 32 GB
quota killed and the one that the Curve rebuilds queued behind: started
16:54:24 UTC, committed 19:34:20, 9,606 seconds, well inside the
replica's four hours, with nothing else writing to the database. The
job's own line says what the plan said on 2026-11-04 to the row: 41
files, 1,309,804 lines, 0 rehashed, curated kept 0, retired 1,309,804
as `retyped`, inserted 1,309,804 (batch
`bat_1793897664113_000000_e7c8d5a1`, run outcome COMPLETED, no run
left open).

Read back from the laptop afterwards, counts and totals only:

| Check | Value |
|---|---|
| landed lines | 1,309,804 |
| typed rows | 1,309,804 |
| curated rows, all versions | 2,619,608 |
| current rows | 1,309,804 |
| retired as `retyped` by the batch | 1,309,804 |
| inserted by the batch | 1,309,804 |
| identities with two current rows | 0 |
| `amount`, current rows | 194,205,717.26 |
| `amount`, retired rows | 194,205,717.26 |
| `amount`, typed | 194,205,717.26 |
| `fee`, current = retired = typed | 1,106,575,137.32 |

So the ledger holds every payment line twice, once under the old key
and once under the new, the old version retired and the new one
current, which is what D18 chose over an in-place rewrite: the history
of the key change is in the table. The cost is the one measured in
"Where the bytes go": Monthly Payments' curated layer doubled, and the
version store the transaction built is the database's to clean up
over the following hours (the count of the retired rows took 142
seconds against 3 for the current ones, the hash index still carrying
the in-row versions). The developer's standing instruction, scale to 8
vCores if it failed again, was not needed.

One thing to know about the launcher. Its `--wait` poll from the
laptop lost its connection to the Container Apps API after two hours
("An existing connection was forcibly closed by the remote host", at
18:57 UTC) and the launcher exited 1 while the execution went on
unaffected; a plain status loop re-attached and saw it end. The
execution's state is the job's, never the launcher's, and the record
of what it did is `meta.batch` and the console log, which is where the
result above was read from. A database-side probe that listed
`tempdb.sys.tables` blocked for the last 35 minutes of the transaction
on the staged `#target` table's metadata and returned the moment the
commit released it; a probe that wants to watch a running rebuild
reads `sys.dm_exec_requests` and `sys.dm_tran_database_transactions`
and stays out of tempdb's catalog.

With this, every rebuild queued since D18 was built is in Azure: the
Monthly Payments key, the sixteen added columns, the ABC key, the
Curve money columns. The same evening the developer said "Go ahead
and move the images yourself", and both jobs now run e7a5161, the
image with the pricing variant. The registry holds eight tags, one per
commit that was built (plus a stale `latest` from 2026-10-23), 723 MB
in all against the Basic tier's 10 GB; a tag is kept so any one-off
execution can name the exact code it ran on, and the older ones can be
deleted whenever that record is no longer wanted.
