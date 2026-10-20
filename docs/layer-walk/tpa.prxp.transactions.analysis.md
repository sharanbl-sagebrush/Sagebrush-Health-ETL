# Review: tpa.prxp.transactions

Reviewed 2026-10-15 against `docs/layer-walk/tpa.prxp.transactions.md` (generated the same day) and
`specs/tpa.prxp.transactions.yaml`. Three distinct deliveries, all xlsx, one sheet each; a fourth file is a
byte-identical copy of the history file in a second folder and is correctly refused. Deliveries in as-of
order: D1 = 01012023-03062026 (as-of 2026-03-08), D2 = 01012026-04052026 (2026-04-06),
D3 = 03012026-06102026 (2026-06-11).

## Verdict

Loadable, but not safe to present as-is, for three reasons that are all spec work, not loader work.
(1) D3 renamed two headers (`Referral Type` to `Referral Type List`; `Waiting for Check DateTime` split into
`Waiting for Check Date` plus a new `Waiting for Check Time`). With no alias in the spec, all 402 rows shared
between D2 and D3 are "corrections", 30 for no other reason, and all 1,183 current rows from D3 have a null
`referral_type` although 402 had a value in D2. (2) D3 adds `Patient Email`, populated on 1,071 of 1,183
rows; under `on_new_column: warn` it lands with no PII class, and `patient_date_of_birth`, `patient_id` and
`patient_external_id` carry none either. (3) Nine columns hold money; the spec types one as currency. The
dedup rule itself behaves: the key is unique in every file, corrections are real vendor edits (newer `Date
Modified` on 947 of 1,021), the late replay changes nothing, and the current count equals the union of
identities. Ten current rows (16,852.47 of `total_amount_collected`) are vanish candidates for the unbuilt rule.

## Deliveries and as-of

- All three carry a `docProps/core.xml` creation stamp; the application is Microsoft Excel, not a portal.
  D1's modified stamp is 6 minutes after created with a `lastModifiedBy` present, so D1 was opened and
  re-saved by a person; D2 and D3 have created == modified and no author fields.
- Stamps are UTC and fall in the first hour of the day after the filename end date for D2 and D3 (the
  vendor's local evening of the filename date); D1's is 03-08 (local 03-07) against a filename end of 03-06.
  The walk's as-of is one calendar day after the local extraction day in every case; ordering is unaffected.
- D1's data does not stay inside its filename coverage: 13 rows have `Rx Fill Date` 2026-03-07 and 83 have
  `Date Modified` 2026-03-07, inside the stamp. D2 and D3 have 0 rows past their filename end. No delivery
  has any fill, modified, completed or waiting date after its as-of. `Date Written` precedes the filename
  start on 267 rows of D2 and 338 of D3; fill date is never before written date (0 of 7,327 rows).
- Overlaps by fill date: D1-D2 2026-01-01..03-06 (700 identities); D2-D3 03-01..04-05 (402); D1-D3
  03-01..03-06 (94, all also in D2). Fill date is not immutable: it moved later on 8 shared rows between D1
  and D2 (max 14 days) and on 14 between D2 and D3 (max 53 days), every one leaving the older window.

## Landed

- Header width 90 / 93 / 94; the common columns keep the same order. D2 adds `Primary Group ID`,
  `Secondary Group ID`, `Tertiary Group ID` (non-blank on 911, 130, 0 rows). D3 adds those three plus
  `Patient Email` (1,071 non-blank), `Referral Type List` and `Waiting for Check Time`, and drops
  `Referral Type` and `Waiting for Check DateTime`; the walk reports those two as missing optional columns,
  which is right under the spec as written. No prohibited columns.
- Physical data rows equal the walk's landed lines: 5,133 / 1,011 / 1,183; 0 interior blanks; full width; no footer.
- The loader records extra column names only today; "kept as extra_cells" in the walk is the design, not code.

## Typed

- 0 quarantined rows in any delivery; no spec change is needed to parse. The placeholder string in the money
  and date columns (one distinct string per column, in the parser's nullish set) lands as null.
- Money columns, by sampling cell values (all native xlsx int/float cells, never symbol text): `Patient Paid
  Amount`, `Primary Paid Amount`, `Secondary Paid Amount`, `Tertiary Paid Amount`, `CE Responsibility`,
  `Total Amount Collected`, `Acquisition Cost`, `WAC`, `AWP`. Only `total_amount_collected` is typed
  currency; the other eight are text. All nine parse as currency with 0 failures in all three files. The paid
  amounts, CE responsibility, total and acquisition cost never exceed 2 decimals; `WAC` and `AWP` carry 3-4
  decimals on 1,087 and 1,431 D1 rows (reference prices, not extended amounts). On every row of every file
  patient + primary + secondary + tertiary paid equals `Total Amount Collected` (7,327 of 7,327). No negative
  value anywhere. Recommended: all eight `currency`; control_total on the four paid amounts,
  `ce_responsibility` and `acquisition_cost`; `wac` and `awp` without control_total.
- Native datetime cells bypass `date_formats` here too: `Date Modified`, `Completed DateTime` and `Waiting
  for Check DateTime` carry a non-midnight time on 100% of populated rows, are typed `date`, and lose it
  silently; the spec comment saying such rows quarantine is wrong for xlsx. After typing,
  `completed_date == completed_datetime` on every row. Measured effect: 0 shared rows in any pair differ
  only by time of day, so nothing is hidden in these files.
- `Days Supply Ends On` is a native datetime cell typed text, landing as a "date 00:00:00" string on all 7,294
  populated rows; it should be `date`. It equals fill date + days supply on only 2,254 of 5,117 D1 rows.
- `Rx Number`, `Days Supply`, `Refill Allowed`, `Refill Number`, `Total Rx Count` are native integers typed
  text; `Total Rx Count` is 1 on every row. `Quantity` (text) equals `Qty Dispensed` on 7,201 of 7,327 rows.
  NDC: every cell is a 13-character hyphenated form normalising to 11 digits; 0 blank.
- The D3 drift is a rename, verified on the 402 shared rows: `Referral Type List` equals D2's `referral_type`
  on 402 of 402; `Waiting for Check Time` (a native time cell) equals the time part of D2's `Waiting for
  Check DateTime` on 366 of 367. Spec change: add `Referral Type List` as a second source of `referral_type`
  and a `waiting_for_check_time` text target sourced from `Waiting for Check Time`.

## Curated

- `rx_transaction_id` is unique in every delivery (5,133 / 1,011 / 1,183 distinct), 36 characters on every
  row, and 1:1 with (`rx_number`, `refill_number`) in every file. Identity is right; no within-file groups.
- D1 to D2: 700 shared, 175 identical, 525 corrected. Differing columns (rows): `date_modified` 494,
  `rx_status_type` 369, `days_supply_ends_on` 315, `primary_payer_name` 68, `primary_plan_type` 56,
  `transaction_status` / `completed_date` / `completed_datetime` 54 each, `shipper_name_tracking_number` 42,
  `waiting_for_check_date` / `_datetime` 42 each, patient and delivery address/city/state/zip 11-20 each,
  `ooc_flag` 15, `rx_fill_date` 8, `patient_class` 5, and 1-2 rows each in `ndc`, `labeler_name`,
  `acquisition_cost`, `wac`, `proprietary_name`, `non_proprietary_name`, `patient_external_id`,
  `primary_paid_amount`, `total_amount_collected`. 50 rows differ in `date_modified` alone.
- D2 to D3: 402 shared, 0 identical, 402 corrected; 30 differ only in the two renamed columns. Excluding
  those, the profile matches D1-D2 (`date_modified` 359, `rx_status_type` 303, `days_supply_ends_on` 218,
  `rx_fill_date` 14, `patient_id` 3, `patient_first_name` 2, one money row). D1 to D3: 94 shared, 94 corrected.
- The 31 (D1-D2) and 13 (D2-D3) corrected rows whose `date_modified` is not newer differ only in patient
  address, payer name, plan type, patient class or external id: master data joined at extraction time, so an
  old transaction "corrects" when the patient moves. Right identity, but the row hash covers master data.
- Vanish candidates: 7 D1 rows filled in 2026-03 absent from D2 and D3 (all sharing one `transaction_status`
  held by 43 D1 rows) and 3 D2 rows filled in 2026-04 absent from D3; none reappears under another key with
  the same (`rx_number`, `refill_number`). `total_amount_collected` 14,545.21 and 2,307.26.
- Late replay of D1: 525 late, 4,608 unchanged, 0 current rows changed. Correct.

## Presented

- 6,225 current rows equals the union of identities (6,225). By fill year: 2023 124, 2024 1,302,
  2025 3,000, 2026 1,799; 2026 by month 309 / 297 / 353 / 320 / 351 / 169.
- Taking each fill month from the newest delivery covering it gives 6,215 rows and 11,312,152.49; the
  current set is 10 rows and 16,852.47 higher, exactly the 10 vanish candidates.
- 1,183 current rows (all from D3) have null `referral_type`; 402 of them had a value in D2. Shape only:
  `transaction_status` 6,153 / 38 / 34 over 3 values; one covered entity, one facility, two pharmacies.

## Proposed event-date column

`rx_fill_date`. Populated on every row, a native date cell with no time part, never after the as-of, and the
column the vendor's filter is applied to (0 rows outside the filename window in D2 and D3; D1's 13 rows on
03-07 are inside its stamp). `date_modified` moves on every vendor touch (947 of 1,021 corrections). Vanish
caveats: a fill date can move later by up to 53 days and out of the older window, and D1's real coverage
ends 03-07, not 03-06, so coverage end should be max(filename end, latest event date) or the local as-of.

## Open questions for the developer

1. Add `Referral Type List` as a second source for `referral_type` and a new `waiting_for_check_time`
   text target for `Waiting for Check Time`: yes/no?
2. Re-run D3 after that change so the 402 corrections become 372 and the 1,183 null `referral_type`
   current rows are refilled: yes/no?
3. Declare `patient_email` in the spec rather than letting it land with no class: named decision
   (`direct` / `prohibited`).
4. Set `pii: direct` on `patient_date_of_birth`, `join_key` on `patient_id` and `patient_external_id`,
   `quasi` on patient and delivery zip/city/state and `patient_gender`: yes/no?
5. Retype the eight text money columns as `currency` (control_total on the four paid amounts,
   `ce_responsibility`, `acquisition_cost`; not on `wac`, `awp`), `days_supply_ends_on` as `date`, and
   `rx_number`, `days_supply`, `refill_allowed`, `refill_number`, `total_rx_count` as `integer`: yes/no?
6. Declare `rx_fill_date` as the event-date column: yes/no?
7. For the vanish rule, take coverage end as max(filename end, latest event date) so D1's 03-07 rows count
   as covered: yes/no?
8. Exclude the master-data columns (patient and delivery address/city/state/zip, payer name, plan type,
   patient class) from the row hash so a demographic update does not supersede old transactions, or keep
   them so the current row shows current demographics: named decision.
9. Fix the spec comment on the `*DateTime` columns (xlsx datetime cells do not quarantine; the time is
   dropped) or add a `datetime` type so `date_modified` keeps its time: named decision.
10. Treat the 7 D1 rows sharing one `transaction_status` that vanished from D2 and D3 as reversals (retire
    on vanish) or as a filter difference (keep): named decision.
11. D1 was re-saved in Excel after creation: trust its stamp as the extraction time, or require an
    operator-supplied as-of for hand-touched files: yes/no on trusting it.

## Probes run

- Read `docProps/core.xml` and `app.xml` of each workbook: created/modified stamps, application name, presence and equality of creator and lastModifiedBy.
- Streamed each sheet with openpyxl: header width, blank or duplicate names, physical non-blank rows, interior blanks, row widths; diffed the three header lists; per column Python cell types, and for datetime cells non-midnight count, min, max, rows after as-of or outside the filename window.
- Classified string cells in money and date-like columns as nullish or other; counted decimal scale of float money cells, non-blank `Patient Email` and group-id cells; classified key and NDC cells by shape and length.
- Loaded each file with the real loader: outcome, counters, drift; per typed date column min, max, nulls, rows after as-of or outside coverage; fill-vs-written and modified-vs-fill ordering; distinct counts of status and site columns; parsed the nine money columns with `parse_currency` (ok, fail, null, negative, zero, sum); tested paid-amount sum against the total, `quantity` against `qty_dispensed`, `total_rx_count` equal to 1, `days_supply_ends_on` against fill date plus days supply.
- Keyed each file by identity and occurrence; per delivery pair counted shared, identical and differing rows, differing columns, rows differing only in absent optional columns or only in `date_modified`, rows whose `date_modified` is not newer, fill-date moves and whether they left the older window, and older-only rows inside the newer coverage with status frequency, month, money sum and re-keying under the same (rx number, refill number).
- Compared D2's `referral_type` with D3's raw `Referral Type List`, and the time part of D2's raw `Waiting for Check DateTime` with D3's raw `Waiting for Check Time`, on shared rows; compared raw datetime cells of the three `DateTime`-style columns across shared rows (identical, same day different time, different day).
- Replicated the dedup rule in memory: current count, by delivery, by fill year and month, money total; compared with a fill-month-partitioned union from the newest covering delivery and bucketed the leftovers; counted current rows with null `referral_type` that had a value earlier; replayed D1 last.
