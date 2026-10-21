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
