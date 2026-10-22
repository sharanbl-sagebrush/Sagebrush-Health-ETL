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
