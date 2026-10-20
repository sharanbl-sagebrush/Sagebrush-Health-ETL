# Review: dispense.weinfuse.administered

Reviewed 2026-10-15 against `docs/layer-walk/dispense.weinfuse.administered.md` (generated the same day) and
`specs/dispense.weinfuse.administered.yaml`. Nine files found; three are distinct Administered deliveries, in
as-of order: D1 = Jan012025-Feb052026 CSV (as-of 2026-02-05), D2 = 01012023-03062026 CSV (2026-03-06),
D3 = 03012026-06102026 XLSX (2026-06-10). The other six: two byte-identical copies of D2's folder, three BuyBill
files caught by this spec's filename pattern, and one misnamed BuyBill file (see Deliveries).

## Verdict

Loadable, not yet safe to interpret. Every real delivery binds, zero rows quarantine, the natural key is unique
in all three files, the late-file replay changes nothing, and the current-row count reconciles with a
month-partitioned union to within the 3 rows the unbuilt vanish rule would retire. Three decisions are needed
before the curated layer means anything: (1) `created_at` is text and the two modules (plus an Excel pass over
D1) format it differently, so 19,641 of the 19,644 D1-to-D2 "corrections" and all 411 D2-to-D3 corrections are
formatting, not vendor edits; (2) legacy `Created At` and new-module `Created Date` are different timestamps
(one follows the administration, the other precedes it), so one target conflates two fields; (3) the
Administered report is a strict superset of the BuyBill report (45,013 of D2's 51,178 rows are the BuyBill file,
identical on all 28 common columns), so loading both sources and summing double counts 2,346,719 of 2,830,423
billing units. The spec comment saying the two exports hold different populations is inverted.

## Deliveries and as-of

- As-of: D1, D2 from the filename end date; D3 from the xlsx `dcterms:created` stamp (2026-06-10, creator Apache
  POI, i.e. the vendor portal), equal to its filename end date. 0 rows after as-of in every file. All correct.
- D3 `dcterms:modified` is 2026-06-26 by the operator in Excel Online. Irrelevant to as-of (the created stamp
  survives a re-save) but relevant to integrity: the stored hash is of the re-saved workbook, so a re-download of
  the same extract arrives with a new hash and the same as-of and is adjudicated row by row. The 411 rows D3
  shares with D2 agree on every business column, the only evidence the content was not altered.
- D1 is not pristine either: CRLF, `Date`/`Expiration` in `m/d/Y` where D2 is ISO, `Strength` and `Unit Price`
  with trailing zeros stripped (2,636 integer-shaped prices), `Created At` cut to `m/d/Y H:M`. It passed through
  Excel and sits at the ETL root, not in a dated folder. Typed values survive; the text columns do not.
- Coverage: D1 holds 31 rows dated 2024-12-31, the day before its filename start, and 0 on 2024-12-30 (D2 has
  62), so the vendor's start filter spills one day. D2 and D3 have 0 rows before start. The as-of day itself is
  incomplete at extraction: 2026-02-05 has 69 rows in D1, 95 in D2; 2026-03-06 has 12 in D2, 17 in D3; D3 has 130
  on 2026-06-10 that the next extract will extend.
- The 2026-04-05 Administered extract was never received: `WeInfuse_AdministeredDrugsReport 01012026-04052026.csv`
  is byte-identical to the BuyBill file beside it and carries the BuyBill header (`Vendor`, `Location Name`). The
  header fingerprint rejects it. No date gap results (D2 runs to 03-06, D3 from 03-01).
- `weinfuse.*administereddrugs(?!.*buybill)` matches every BuyBill name because `buybill` precedes
  `administereddrugs`; three BuyBill files are walked and reported UNRECOGNISED. Harmless now, noisy under
  alerting. `^(?!.*buybill).*weinfuse.*administereddrugs` matches the Administered names only (probed). Not changed.
- Event date: `administration_date` (sources `Date` / `Appointment Date`); see the proposal.
- Vendor timestamp: `Created At` / `Created Date`, landed as `created_at` text. Later than the event date on
  1,101 of 19,645 rows (D1), 4,763 of 51,178 (D2; 1,988 by more than 30 days, max 904), 63 of 5,671 (D3). Not a
  row version: the 3 real D1-to-D2 edits left it equal to the minute (a creation stamp, not last-updated), and in
  D3 it precedes the administration on 5,325 of 5,671 rows (order creation, a different field). On the 411 shared
  D2/D3 rows it agrees to the day on 43 and to the minute on 0. File as-of stays the version.

## Landed

- Headers: D1 and D2 identical (33 columns, same order). D3 has 35: an unnamed row-number column at position 0
  (dropped by the junk-header rule, so not reported as extra), `Inventory Type` and `Date of Birth Date` added,
  14 renamed as the spec comment lists. No extra, missing-optional or prohibited columns anywhere. BuyBill
  files differ from legacy Administered only by `Vendor`/`Location Name` for `Type`/`Location`, as the detectors assume.
- Physical lines after the header equal landed data lines: 19,645, 51,178, 5,671 (xlsx max row 5,672 with
  header). 0 blank records, every CSV record 33 cells, no BOM; D1 CRLF, D2 LF.
- D1 and D2 bind to `legacy_export`, D3 to `reporting_module_2026`; both land on the same 35 targets (legacy
  lands `date_of_birth` null).

## Typed

- 0 quarantined rows anywhere; no spec change needed for parsing. Nulls: `billing_code_hcpcs` 453 / 1,283 / 111,
  `lot` 1 / 1 / 0, `unit_price` 10 in D3, `date_of_birth` on every legacy row.
- Native xlsx datetimes: `Appointment Date`, `Expiration Date`, `Date of Birth Date`, `Created Date` are midnight
  datetimes (0 non-midnight), so `.date()` loses nothing for the `date` targets. The text targets are hit:
  `administration_time` and `created_at` arrive as `str(datetime)` (`YYYY-MM-DD HH:MM:SS`) where the legacy CSV
  carries `2000-01-01 HH:MM:SS UTC` and `YYYY-MM-DD HH:MM:SS -0700/-0800`. Same value, different hash input.
- Time zone: legacy `Time` is labelled UTC but its hours run 07-16 (clinic hours); new-module `Appointment Time`
  runs 12-23 and sits +8 h (279 rows) or +5 h (132) from the legacy value on shared rows, so one is UTC, the other
  local, and at least two clinic zones exist. `Appointment Date` equals the date part of `Appointment Time` on all
  5,671 D3 rows; no shift observed, but an evening administration could land on the next UTC day.
- Numbers: `unit_price` never negative, zero on 2,553 / 6,165 / 726 rows; `billing_units` never negative, integer
  everywhere, zero on 453 / 1,292 / 111 rows, coinciding with null `billing_code_hcpcs` (453 of 453, 1,283 of
  1,292, 111 of 111) and the smaller `inventory_type` group. NDC: every inner and outer value is 11 plain digits;
  inner equals outer on 16,865 / 44,016 / 4,993 rows. `Provider NPI` is 10 digits on every legacy row. Control
  total `billing_units` 1,014,903 / 2,830,423 / 271,207; no vendor footer to check.
- PII: `patient_first_name`, `patient_last_name`, `date_of_birth` are `direct`; `patient_identifier`,
  `weinfuse_uid` are `join_key`; no other column carries a patient identifier.

## Curated

- (`order_id`, `inventory_item_id`) unique in every delivery (19,645 / 51,178 / 5,671 identities, 0 groups), as
  is (`appointment_id`, `inventory_item_id`). Max occurrence 1; numbering never matters.
- D1 to D2, 19,644 corrections: `created_at` text differs on all, agrees to the minute on 19,643. Real edits:
  `lot` 2, `billing_units` 1 (newer lower), `created_at` 1 (139 h apart, nothing else changed). `strength`
  (19,617) and `unit_price` (4,248) differ in Decimal exponent only and hash equal. 19,641 spurious supersessions.
- D2 to D3, 411 corrections: `administration_time` 411 (format, zone), `created_at` 411 (different field),
  `date_of_birth` 411 (new column), `patient_first_name`/`patient_last_name` 54 (all equal after case fold, 15
  patients, same `patient_identifier`), `label_name` 2 (real). No key, date, NDC, HCPCS, unit or price changed.
  The module switch re-supersedes every overlapping row; the DOB part is unavoidable under the all-columns hash.
- Identity holds: `patient_identifier` and `weinfuse_uid` agree 411 of 411; the 3 real edits are in-place, not
  reorderings.
- Overlaps: D1/D2 window 2025-01-01..2026-02-05: 41 identities only in D2 (33 in the final week, 31 on orders D1
  had, 13 created after D1's as-of: late entries); 1 only in D1 (2026-02, order still in D2): a true vanish.
  D2/D3 window 03-01..03-06: 13 only in D3, 2 only in D2 (both minority appointment status, orders absent from
  D3). D3 has one appointment status; legacy files have two (5 and 12 minority rows), so the new module drops the
  minority status and those 12 rows will never be superseded.
- Late-file replay of D1: 19,644 late, 1 unchanged, 0 current rows moved. Correct.
- Locations: 9 in D1/D2, 8 in D3; 2 legacy locations absent from D3 (154 rows, last seen 2026-02), 1 new in D3.

## Presented

- 56,439 current rows = 56,436 from taking each month from the newest covering delivery, plus the 3 vanish
  candidates. `billing_units` 3,082,108 current vs 3,081,668 partitioned. By year: 2023 13,712; 2024 16,420;
  2025 17,785; 2026 8,522. Months 2023-01..2026-02 come from D2, 2026-03..06 from D3; D1 keeps 1 orphan row.
- Cross-source: BuyBill 0306 (45,013 rows) and 0205 (17,089) are subsets of D2 and D1, 0 BuyBill rows outside
  them, identical on all 28 shared targets; Administered-only rows (6,165 / 2,556) are the zero-price
  `inventory_type` group. Summing both `dispense.weinfuse.*` sources counts 2,346,719 units twice in D2's period.
  The sibling spec also names the same four columns differently (`date`, `time`, `location_name`, `vendor`).

## Proposed event-date column

`administration_date`. The only per-row date of the clinical event, required and populated on every row, parses
in both modules (ISO in D2, US in D1, native datetime in D3), never exceeds the as-of, equals the date part of the
new module's `Appointment Time` on every row, and is what the vendor filter is applied to (the 31-row spill is
one day at the start, never at the end). `created_at` trails the administration by up to 904 days in the legacy
module and precedes it in the new one; `expiration` and `date_of_birth` are attributes. For the vanish rule:
scope coverage by location as well as date, and allow the one-day start spill.

## Open questions for the developer

1. `created_at`: add a timestamp type with offset-aware formats, or exclude it from the row hash? Named decision.
2. Split `Created Date` (order creation) from `Created At` (record creation) into two targets: yes/no?
3. Declare `administration_date` as the event-date column: yes/no?
4. Treat `dispense.weinfuse.buybill` as a subset of this source and never sum the two: retire it or mark it
   derived? Named decision.
5. Rewrite the filename pattern with a start-anchored negative lookahead: yes/no?
6. Was the 03012026-06102026 workbook edited in Excel Online on 2026-06-26, or only re-saved? If unknown,
   re-download: yes/no on re-download.
7. Was a 2026-04-05 Administered extract ever taken (the folder holds two copies of BuyBill)? Yes/no.
8. Exclude the minority appointment status (17 legacy rows, dropped by the new module) from reconciliation: yes/no?
9. Scope vanish coverage by (`administration_date`, `location_id`): yes/no?
10. Which zone is the new module's `Appointment Time` in, and is `Appointment Date` derived from it? Named answer.
11. Fold case in `patient_first_name`/`patient_last_name` before hashing (54 case-only corrections): yes/no?

## Probes run

- Raw CSV bytes per file: BOM, line endings, physical lines after header, record count and widths, blank records; xlsx `core.xml`/`app.xml` stamps, sheet names, row count, Python cell type per column; header name and order diff across all seven distinct files.
- Real loader on D1, D2, D3: outcome, variant, drift, min/max `administration_date`, rows after as-of and before start, rows per year and month, null counts, distinct status/type/location/group counts, Decimal exponents, NDC lengths, sign and zero counts.
- Shape classification of `created_at` and `administration_time` text and offset suffixes; `created_at` parsed and compared with `administration_date` and the as-of, with lag buckets.
- Non-midnight native datetime cells per xlsx column; `Appointment Time` date part vs `Appointment Date`; hour histograms per module and hour offset on shared rows.
- Keyed every file by (`order_id`, `inventory_item_id`) and by (`appointment_id`, `inventory_item_id`); duplicate group counts.
- Per delivery pair: shared identities, hash-identical rows, differing columns by equality and by repr, `created_at` agreement to the minute and day, name and label equality after case fold, window-only identities with lag, month, status, order and location breakdowns.
- Dedup rule replicated in memory: current rows by setter and year, `billing_units` sum, month-partitioned union, D1 replayed last.
- Spec filename pattern and a start-anchored alternative tested against the three filename shapes.
- BuyBill files loaded with the BuyBill spec: shared identities with D1/D2, agreement on common targets, `inventory_type` and `unit_price` profile of Administered-only rows.
- Location sets diffed across deliveries; rows at locations absent from D3 counted by year.
