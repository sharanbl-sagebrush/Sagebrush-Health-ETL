# Review: tpa.wellpartner.claims

Reviewed 2026-10-16 against `docs/layer-walk/tpa.wellpartner.claims.md` (generated 2026-10-15) and
`specs/tpa.wellpartner.claims.yaml`. Four distinct deliveries; a fifth file is a byte-identical copy of the
history workbook and is correctly refused. In as-of order: D1 = Jan012025-Feb052026.csv (2026-02-05,
filename), D2 = 01012023-03062026.xlsx (2026-03-08, stamp), D3 = 01012026-04052026.xlsx (2026-04-06, stamp),
D4 = 03012026-06102026.xlsx (2026-06-10, stamp). Same portal as `tpa.wellpartner.invoices.analysis.md`.

## Verdict

Safe to load as-is: every delivery binds to the one 119-column variant, zero rows quarantine, `transaction`
is unique in every file, there are zero conflicts, the late-file replay changes nothing, and no financial
column ever differs between deliveries on a shared row. Not ready for reconciliation until three things are
decided. (1) The extract carries one `transaction_status` only; reversed or reprocessed claims leave the
extract instead of changing status, so without the vanish rule the presented layer keeps 22 claims
(program_revenue 28,808.90) that D2, D3 and D4 no longer carry. (2) Vanish cannot use the fill date or the
filename: the portal filters on `processed_date`, D3's filename claims 2026-01-01 but holds nothing processed
before 2026-03-13, and a filename-window vanish on D3 would retire 1,141 current D2 rows. (3) 2,518 of the
2,940 D1-to-D2 corrections are CSV-versus-xlsx representation noise, and the CSV's `gpi` is in scientific
notation on 2,370 of 2,962 rows, a lossy, irrecoverable defect of that delivery.

## Deliveries and as-of

- Stamps vs filename end dates: D2 created 2026-03-08T00:40Z, modified 18 minutes later (re-saved before
  upload, like the invoices D2), filename 03-06, latest `processed_date` 03-07 (11 rows). D3 created
  2026-04-06T00:38Z (US evening of 04-05), filename 04-05, latest `processed_date` 04-04. D4 created
  2026-06-10T23:31Z, filename 06-10, latest `processed_date` 06-10. D1 is CSV: filename only, latest 02-05.
- Data stays inside every as-of: 0 rows after as-of in any of the six date columns, in any delivery.
- The portal's filter column is `processed_date`: 0 rows outside the filename window on it in D1, D3, D4;
  D2 has 11 rows one day past its end. `dispensed_date` (the fill date) precedes the filename start on
  196 / 319 / 1 / 494 rows (D1-D4) and lags `processed_date` by over 90 days on 51 / 895 / 1 / 269 rows
  (0-7 days on 1,185 / 23,576 / 153 / 667); it is never after `processed_date`.
- D3 does not fill its filename window: `processed_date` runs 2026-03-13..04-04 (282 rows in March, 81 in
  April). D2 holds 1,141 rows processed 2026-01-01..03-06 that D3 lacks; D4 holds 84 rows processed
  03-08..03-12 that no other delivery carries. D3 is a "since the last extract" pull, as for invoices.
- Overlaps by `transaction`: D1 x D2 2,940 shared (0 hash-identical); D2 x D4 224 (3 identical); D3 x D4
  363 (93 identical); other pairs 0. Only D1 x D2 has older-only identities in the shared window (22, Curated).
- `last_modified_date_time` as row version: text; CSV `%m/%d/%Y %H:%M` (2,643 rows), xlsx `%m/%d/%Y %I:%M %p`
  (41,545 / 164 / 1,704); blank on 319 / 473 / 199 / 282 rows, and every blank row also lacks
  `last_allocation_date` and `invoice`: it is the replenishment-lifecycle stamp, not a row stamp. Where present
  it is later than `dispensed_date` on 100% of rows, never after the as-of, earliest 2025-05-03 even in D2. On
  raw-differing shared rows it moved forward on 234 / 13 / 94 (D1-D2, D2-D4, D3-D4), was null on one side on
  311 / 208 / 176 and equal on 2,395 / 0 / 0. It cannot order deliveries; file as-of stays the version.
- Covered-entity ids (`n340b_id`): 2 in D1 and D2, 1 in D3 and D4. Contract pharmacies 65 / 89 / 44 / 48.

## Landed

- Header: 119 columns, identical in name and order across all four deliveries (0 added, 0 removed). No
  extra, missing-optional or prohibited columns. CSV and xlsx both bind to `claims_119` on the same 119 targets.
- Physical rows equal the report's landed lines: D1 2,962 CSV records after the header (no BOM, CRLF, every
  record 119 cells, 0 blank); D2 / D3 / D4 one sheet each, `max_row` = data rows + 1, 0 blank rows.
- Raw workbook cell types: all 11 date-like headers are text in m/d/Y (0 datetime cells); the key, `GPI`,
  `Card Holder #`, `Qty Allocated`, `Rx #`, `NDC` are text; `Est Wholesale Cost` is float/int.
- Representation drift between CSV and workbooks, all in text-typed columns: `gpi` is scientific notation on
  2,370 CSV rows (and 481 / 1 / 22 xlsx rows); `card_holder` on 295 CSV rows (0 in xlsx); `qty_allocated` is
  a bare integer in the CSV but `N.0` in every workbook. `ndc` keeps leading zeros in all four.

## Typed

- 0 quarantined cells in any delivery. No spec change is needed to load these files.
- The native-datetime bypass does not bite: 0 workbook date cells are datetimes; all go through `date_formats`.
- Control totals: no vendor footer, so the walk report's sums are unverified. The identity
  `est_entity_net_savings = program_revenue - est_340b_cost - admin_fee - dispensing_fee` holds on every row
  of every delivery; `program_revenue` is 0 on 0 rows; `est_entity_net_savings` is negative on
  630 / 19,062 / 43 / 253 rows. `est_wholesale_cost` carries up to 7 decimal places in CSV and xlsx alike
  (vendor precision); on 27 shared D1-D2 rows it differs by less than 0.01, never by 0.01 or more.
- `transaction_status` and `transaction_type` have one distinct value per file; `reversal_method` is always
  null; 0 rows carry a reversal word. Reversals are not represented in the extract.
- Spec-change candidates, named not made:
  - `qty_allocated`, `unreplenished_qty`, `order_attempts` to `decimal`: removes 2,700 `N` vs `N.0` corrections.
  - `last_modified_date_time` to a datetime type with both formats (README D7): removes 2,583 corrections
    that are a 24-hour versus AM/PM rendering of the same stamp.
  - `gpi` shape rule (14 digits, no exponent): exponent-form cells have already lost digits (2,293 D1-D2
    diffs are exponent to 14 digits, 0 recover under normalisation). Quarantine, null or keep: none repairs D1.
  - `card_holder` has no PII class; it is an insurance card-holder number.

## Curated

- Key uniqueness: `transaction` unique in every delivery (2,962 / 42,018 / 363 / 1,986 distinct = rows), 19
  alphanumeric characters on every row; the wide key (`rx`, `dispensed_date`, `ndc`, `fill`) is equally
  unique. Within-file duplicate identities: 0 in every file.
- Corrections D1 to D2: 2,940 of 2,940 shared rows differ. 2,518 differ only in representation columns
  (`last_modified_date_time` format, `qty_allocated` `.0`, `gpi` and `card_holder` exponent form,
  `est_wholesale_cost` sub-cent). 422 are real: `last_allocation_date` and `order_attempts` 315,
  `unreplenished_qty` 240, `invoice` and the `voucher` columns 233, `last_allocation_type` 226,
  `orderable_date` 201, `reconciliation_date` 40, `primary_payer_name` 37, `external_eligibility_reason` 35,
  `primary_plan_type` 32, `true_up_inventory_key` 10, batch columns 7, qualification columns 3.
- Corrections D2 to D4 (221 of 224) and D3 to D4 (270 of 363), none representation-only: `invoice` and the
  `voucher` columns 218 / 262, `order_attempts` 206 / 258, `last_allocation_date` 205 / 257, `qty_allocated`
  and `unreplenished_qty` 201 / 229, `orderable_date` 186 / 224, `reconciliation_date` 6 / 29; D2 to D4 also
  `clinic_code` / `clinic_name` / `qualification_date` / `qualification_source` 95, `patient_mrn` /
  `patient_qualification_*` / `prescriber_qualification_npi` / `carve_in_model` 69.
- The nine financial and quantity control columns differ on 0 shared rows in every pair; `transaction_status`
  changes on 0. These are real vendor updates to the replenishment lifecycle (allocation, voucher, invoice,
  reconciliation) and after-the-fact qualification, not a wrong identity. The identity is right.
- Late-file replay of D1: 2,940 late, 22 unchanged, 0 current rows changed. Correct.
- The 22 D1 rows absent from D2 (processed 2025-10: 5, 2025-11: 11, 2026-01: 6; dispensed 2025-08..2026-01)
  are also absent from D3 and D4. 4 reappear in D2 under a different `transaction` with the same wide key
  (reprocessed); 10 share `rx` + `ndc` with a D2 row. They carry `invoice` on 12 of 22 (83% elsewhere in D1)
  and `last_allocation_date` on 7 of 22 (85% elsewhere); 18 have none of `reconciliation_date`,
  `true_up_inventory_key`, `non_replenishment_reason`. Totals: program_revenue 28,808.90, est_340b_cost
  16,455.97, est_entity_net_savings 6,688.68, dispensed_qty 1,444. All 22 are current today.

## Presented

- 43,802 current rows = 43,802 distinct `transaction` values across all four deliveries, the plain union; no
  identity is counted twice. Last set by: D2 41,797, D4 1,890, D3 93, D1 22.
- The 22 D1-only rows are the only current rows a newer, covering delivery does not carry; only the vanish
  rule (not simulated) would retire them. The 1,141 D2 rows inside D3's filename window are correctly current.
- Current totals: program_revenue 51,809,591.90, est_340b_cost 21,755,644.11, est_entity_net_savings
  19,324,807.73 (walk report); net savings is negative on about 45% of rows.

## Proposed event-date column

`dispensed_date` as the event date: the fill date, required, populated on every row, never after the as-of,
and the column a claim is placed in a period by. It must NOT be the coverage column: the portal filters on
`processed_date`, while `dispensed_date` precedes the filename start on up to 494 rows per delivery and lags
processing by over 90 days on 895 D2 rows. In README vocabulary: event date = `dispensed_date`; coverage
over `processed_date` from the data range (D3 claims January, holds 03-13 onward), end = max(filename end,
latest `processed_date`); vanish scope = `n340b_id`; as-of source = xlsx created stamp, then filename. The
spec needs a coverage column distinct from the event date, or vanish stays off and the 22 claims stay current.

## Open questions for the developer

1. Declare `dispensed_date` as the event-date column in the spec: yes/no?
2. Add a coverage column to the spec model and set it to `processed_date` for this source: yes/no?
3. Coverage source for vanish: data range of `processed_date` with end = max(filename end, latest
   `processed_date`), or vanish disabled? Named decision. (The filename window would retire 1,141 D2 rows.)
4. Enable vanish so the 22 reversed or reprocessed claims (28,808.90) leave the presented layer, given the
   extract never carries a reversal status: yes/no?
5. `gpi` in scientific notation (2,370 CSV rows, 504 xlsx rows): quarantine, null, or keep as text? Named
   decision. Was D1 opened and re-saved in Excel before upload (that produces the exponent form)?
6. Retype `qty_allocated`, `unreplenished_qty`, `order_attempts` as decimal and `last_modified_date_time` as
   datetime with both formats, removing 2,518 fake corrections: yes/no?
7. Keep file as-of as the row version and use `last_modified_date_time` only as a check (blank on 199 of 363
   D3 rows, equal on 2,395 differing D1-D2 rows): yes/no?
8. Keep the replenishment-lifecycle columns (`invoice`, `voucher*`, `last_allocation_*`, `order_attempts`,
   `qty_allocated`, `unreplenished_qty`, `orderable_date`, `reconciliation_date`) in the row hash, or exclude
   them (README D6)? Named decision.
9. D2's workbook was modified 18 minutes after creation. Trust the created stamp as extraction time when
   created and modified differ: yes/no? (Same as invoices Q9.)
10. Negative `est_entity_net_savings` on 19,062 of 42,018 D2 rows: expected under the fee structure: yes/no?
11. Assign a PII class to `card_holder`: yes/no?

## Probes run

- Probe 1: workbook `docProps/core.xml` stamps and application; per delivery min/max of every typed date column, rows after as-of and outside the filename window; raw date-like cell shapes and Python types; CSV BOM, line endings, record widths, blank records; header comparison; `last_modified_date_time` parsed against `dispensed_date` and as-of; distinct values per status column, negative control values, dispensed months; per pair of deliveries shared identities, hash-identical ones, differing columns and combos, one-sided rows in the shared window by month; dedup rule replicated with late replay; current rows against the union of keys.
- Probe 2: exact strptime format per file for `last_modified_date_time`; dispensed-to-processed lag buckets; net-savings identity; decimal places of `est_wholesale_cost`; pairwise diffs after normalising numeric text; last-modified direction on differing rows; the 22 CSV-only rows' reappearance under the wide key, months, totals and populated-column rates; D4 rows in the 03-08..03-12 gap; header order across files.
- Probe 3: shape classes of `gpi`, `card_holder`, `qty_allocated`, `ndc`, `rx`, `transaction` per delivery; raw openpyxl cell types for seven columns; D1-D2 shape transitions and digit-normalisation recovery; corrections surviving once representation columns are set aside, per pair; blank-rate of `last_modified_date_time` against allocation columns; negative net-savings and zero-revenue counts; status columns and dispensed months of the 22 CSV-only rows.
