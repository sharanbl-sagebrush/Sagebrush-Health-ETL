# Review: tpa.pharmaforce.claims

Reviewed 2026-10-15 against `docs/layer-walk/tpa.pharmaforce.claims.md` (generated the same day) and
`specs/tpa.pharmaforce.claims.yaml`. Seven distinct CSV deliveries; two more are byte-identical copies of the
2023-2026 pair in a second folder and are correctly refused. Deliveries below in as-of order, with the site
suffix FM (Fire Mesa) or SO (Southington): D1 = `ClaimReports Jan012025-Feb042026` (as-of 2026-02-04, FM and SO),
D2 = `Claims 01012023-03062026` (2026-03-06, FM and SO), D3 = `Claims 01012026-04052026` (2026-04-05, FM only),
D4 = `Claims 03012026-06102026` (2026-06-10, FM and SO).

## Verdict

Safe to load as-is, but the vanish rule must not be enabled for this source until two report-scope questions
are answered. Every file binds to the one variant, header and width are identical across all seven, zero rows
quarantine, `claim_id` is unique within every file and never collides across the two sites, the 624
cross-delivery corrections are real vendor repricing and status changes, the late-file replay changes nothing,
and the presented row count equals the union of distinct identities. The problems are in delivery scope, not in
parsing: (1) the February `ClaimReports` extracts are a narrower report than the later `Claims` extracts (they
carry no reversal rows and lack 3,802 rows D2 has for the same fill dates); (2) the June extract carries no
reversal rows at all and lacks 56 March rows the April extract had (9 of them reversals), so a date-scoped
vanish would silently un-reverse claims; (3) two files share one as-of and one table, so vanish must be scoped by
`opaid` or a Southington file would retire up to 194,907 Fire Mesa rows.

## Deliveries and as-of

- All CSV, so as-of comes from the filename end date in all seven; both name shapes parse. Within every file
  `fill_date` has 0 rows after the as-of and 0 rows before the filename start date. The as-of ordering is right.
- `fill_date` is the vendor's report filter. `claim_date` precedes the filename start on 293 rows (D2 FM) and 24
  (D2 SO) and reaches back to 2020; `claim_captured_date` exceeds the as-of on 289 rows of D2 FM (captured the day
  after the filename end date; all 289 reappear in D3). So the filename end date is the fill-date cut-off, not
  the extraction date. That is fine for ordering as long as the event date is `fill_date`; it would violate the
  CLAUDE.md "as-of not earlier than the latest event date" rule if `claim_captured_date` were chosen.
- D1 (`ClaimReports`) is not a complete extract of its own window. D2 holds 3,802 identities with fill dates
  inside D1's window that D1 lacks: 1,601 reversal rows (D1 has 0 negative-quantity rows in either site file) and
  2,201 positive rows, 2,838 of them more than 90 days old at D1's as-of, spread over 2025-01..2026-02. The
  reverse never happens (0 D1 rows missing from D2). Same 65 headers, different report scope.
- D4 FM carries 0 negative-quantity rows (D2 FM: 17,050; D3 FM: 71). 56 D3 identities with March 2026 fill dates
  are absent from D4 (9 reversals, 47 positive); the other 2,811 shared identities are present. Either the June
  report was run with a narrower filter or those 56 were deleted by the vendor; the data cannot tell which.
- Two files per drop share one as-of and one table. Each file carries exactly one distinct `opaid` and one
  `covered_entity`, 0 identities carry more than one `opaid` across files, and 0 `claim_id` values are shared
  between the two sites (FM 203,948 identities, SO 1,297). A date-only vanish would retire other-site rows at
  every drop: D1 SO would retire 21,954 FM rows, D2 SO 194,907, D4 SO 9,146, D2 FM 372 SO rows. Scope by `opaid`.
- The D3 drop has no Southington file. No delivery holds a Southington row with a fill date in 2026-01-01..04-05
  (Southington rows stop at 2025-07 and resume at 2026-05: 16 rows, then 1 in 2026-06), so the missing file is
  consistent with "no claims", but a missing file and an empty file are indistinguishable to the walk. The spec
  comment that Southington participation ended 2025-07-17 is contradicted by the 17 rows in D4 SO.
- Empty months inside coverage: 2023-01..04 in both D2 files (history starts 2023-05-17); 2025-08..2026-02 in
  D1 SO and 2025-08..2026-03 in D2 SO; 2026-03..04 in D4 SO. Fire Mesa volume by fill year over current rows:
  2023 63,649; 2024 103,114; 2025 23,187; 2026 13,998. The 2025 contraction is a business question.

## Landed

- Header: 65 columns, identical in name and order across all seven files (0 added, 0 removed, 0 duplicated);
  two headers carry a double space, which the spec's sources reproduce. No extra, missing-optional or prohibited
  columns in any delivery.
- Physical lines after the header equal csv records equal landed and typed rows in every file (21,954; 372;
  194,907; 1,280; 7,719; 9,090; 17): 0 blank lines, every record 65 cells, no BOM, CRLF throughout, no footer.
- Entity columns: 0 blank `OPAID` and 0 blank `Covered Entity` cells in any file; exactly 1 distinct
  (`opaid`, `covered_entity`) pair per file. The "two sites via constants" wording in `docs/file-audit.md` and
  `docs/architecture-and-plan.md` is stale; the spec has no `constants` block and needs none.

## Typed

- 0 quarantined cells in any delivery; no spec change is needed for parsing.
- Dates: every populated date cell in every file is US `m/d/Y`; the ISO fallback never fires. `dob`, `fill_date`,
  `claim_date`, `date_rx_written`, `claim_captured_date` are populated on every row. `encounter_discharge_date`
  and `trued_up_date` are blank on every row of every file; `trued_up_units` and `trued_up_cost` are zero
  everywhere, so those two control totals are always 0.
- Money: every cell is dollar-prefixed; negatives use a minus sign (no parentheses anywhere). `qty_dispensed`
  and `days_supply` are bare numbers, negative together on the same rows.
- NDC: every non-blank value is 11 plain digits; 2 blanks in D4 FM (typed as null), none elsewhere.
- `claim_id`: 9 digits, all-numeric, on every row of every file.
- Arithmetic: `patient_pay + third_party_payment = total_payment` holds on every row of every file.
  `n340b_drug_cost + disp_fee = total_claim_cost` fails on 40,092 D2 FM rows and `total_payment -
  total_claim_cost = profit_or_loss` on 45,351, so neither is the vendor's definition; not a parsing fault.
- Reversals: negative-quantity rows carry the one `transaction_code` value positive rows never use (disjoint
  sets in every file that has both), their own `claim_id`, and 16,884 of 17,050 in D2 FM have a positive twin on
  (`prescription`, `refill`, `ndc`, `fill_date`, `pharmacy_nabp_or_npi`); 16,700 were captured within 30 days of
  the fill. Sign is not uniform: 854 reversal rows have positive `third_party_payment` and 1,844 positive
  `profit_or_loss` while `total_payment` is negative on 17,049 of 17,050.
- Control totals: no footer, so the per-file sums are our own; none can be checked against the vendor.

## Curated

- `claim_id` is unique within every file (max occurrence 1, 0 duplicate groups) and across the two sites'
  files at every drop (0 shared between FM and SO in D1, D2, D4, and 0 overall). Identity is right.
- Corrections D1 to D2: 572 rows. Differing columns: `claim_captured_date` 477, `n340b_drug_cost` 316,
  `drug_name` 238, `retail_drug_cost` 162, `profit_or_loss` 135, `total_claim_cost` 132, `replenishment_status`
  29, `e_prescribe_service_location_code` 21, `reason` 14, `claim_sub_type` 12, `prescriber_name` 9, `mfg_name`
  8, six others under 7. Quantity unchanged on all 572, all positive rows, 446 with a money change, concentrated
  in fill months 2025-10..2026-01. Repricing of the 340B cost at the same NDC with a refreshed product
  description and a re-capture date: real vendor adjustments, not a wrong key.
- Corrections D2 to D3: 22 (`replenishment_status` 21, `reason` 2, `retail_drug_cost` 2, three others 1).
  D3 to D4: 30 (`replenishment_status` 25, `reason` 5, `claim_sub_type` 3, `comments` 3, `claim_captured_date`
  3, `ce_receivable` 2, `profit_or_loss` 2, two others 1). D2 to D4 direct: 3, all `replenishment_status`.
  Status progression as replenishment settles. No correction changed `qty_dispensed` or `ndc`.
- Southington: 372 shared identities D1 to D2, all hash-identical; D4 SO shares nothing with earlier files.
- Late replay of D1 FM: 572 late, 21,382 unchanged, 0 current rows changed. Correct.

## Presented

- 205,245 current rows = 205,245 distinct identities across all deliveries (no occurrence > 1 anywhere).
  By site over current rows: Fire Mesa 203,948, Southington 1,297. By fill year: 2023 63,864; 2024 103,807;
  2025 23,559; 2026 14,015. Reversal rows current: 17,062 FM, 2 SO.
- A site-month union taking each month from the newest delivery covering it gives 205,189 rows; the current
  state holds 56 more, worth 281,626.85 of `total_payment`. Those 56 are exactly the March 2026 rows D3 carried
  and D4 does not. Whether they belong in the current state is the D4 scope question; nothing is double counted.

## Proposed event-date column

`fill_date`. It is required and populated on every row, it never precedes the filename start date or exceeds
the as-of in any of the seven deliveries, and the vendor's report filter is evidently applied to it (12 D2 SO
rows with a January 2025 `claim_date` but a 2024 fill date are absent from D1 SO; 165 D2 FM rows with a 2026
`claim_date` but a 2025 fill date are absent from D3). `claim_date` fails the start-date test on 317 rows and
`claim_captured_date` fails the as-of test on 289. Vanish must be evaluated within (`fill_date` window, `opaid`),
and only once the reversal-scope questions below are settled, or a June-style extract without reversal rows
would retire every reversal in its window.

## Open questions for the developer

1. Declare `fill_date` as the event-date column in the spec (field to be added to the model): yes/no?
2. Scope the vanish rule by (`fill_date` window, `opaid`), never by date alone: yes/no?
3. D4 FM has 0 reversal rows and lacks 56 March rows D3 had (9 reversals): was the June report run with a
   different filter? Named decision: which Pharmaforce report (and filter) is the report of record.
4. Keep vanish disabled for this source until question 3 is answered: yes/no?
5. Treat the `ClaimReports` files (no reversal rows, 3,802 rows short of D2 in their own window) as a
   narrower report that may insert and correct but never drives vanish: yes/no?
6. Southington has 17 rows in 2026-05/06 after the spec says participation ended 2025-07-17: is the site
   active again (yes/no), and is a drop with no Southington file to be read as "no claims" or "not extracted"?
7. History starts 2023-05-17 although the files claim 2023-01-01: expected (yes/no)?
8. Accept the filename end date as the as-of even though 289 D2 FM rows were captured after it: yes/no?
9. Are the 854 reversal rows with positive `third_party_payment` and 1,844 with positive `profit_or_loss`
   vendor convention to be summed as-is: yes/no?
10. Classify `e_prescribe_mrn` as `join_key` (it is an MRN, currently `none`) and `comments` as `quasi`
    (free text, populated on 4,896 D2 FM rows): yes/no?
11. Correct the "via constants" wording in `docs/file-audit.md` and `docs/architecture-and-plan.md`: yes/no?

## Probes run

- Parsed each CSV from raw bytes: BOM, line endings, physical lines after header, csv record count, blank lines, record widths, header name and order drift across the seven files, blank `OPAID`/`Covered Entity` cells.
- Classified raw cells of all nine date columns by shape (ISO / US / blank / other), all ten money columns and `Qty Dispensed` by sign notation, and `NDC` by length and hyphenation, per file.
- Loaded each file with the real loader and spec; recorded outcome, variant, new/missing/prohibited columns, quarantine reasons, distinct `opaid` and `covered_entity` counts, `claim_id` distinctness and digit shape, min/max/null per date column with rows after as-of and before filename start, fill-vs-claim date ordering, negative counts per numeric column, and three per-row arithmetic identities.
- Characterised negative-quantity rows: distinct `transaction_code`, `claim_type`, `claim_sub_type`, `reason`, `replenishment_status`, `billing_model`, `drug_indicator` versus positive rows; sign of each money column; positive twin on (prescription, refill, ndc, fill_date, pharmacy); capture lag; presence in the next delivery's window.
- Keyed every file by (identity hash, occurrence); counted cross-site identity overlap per drop and overall, and identities with more than one `opaid`.
- For each delivery pair: shared identities, hash-identical ones, differing columns and column combinations, rows by fill month, quantity and money change flags; identities inside the shared window (by `fill_date` and by `claim_date`) present on one side only, with month and lag.
- Replicated the dedup rule in as-of order; counted current rows per site, per last-setting delivery, per fill year; compared current `total_payment` with a site-month union from the newest covering delivery; listed empty months inside each file's coverage.
- Simulated a `fill_date`-window vanish at each delivery, splitting candidates into other-site and same-site, with reversal counts and source delivery.
- Replayed D1 FM last and counted late / unchanged / changed current rows.
