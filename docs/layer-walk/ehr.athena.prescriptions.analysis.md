# Review: ehr.athena.prescriptions

Reviewed 2026-10-15 against `docs/layer-walk/ehr.athena.prescriptions.md` (generated the same day) and
`specs/ehr.athena.prescriptions.yaml`. Four distinct deliveries, all CSV; two further copies of the 2024-2026
history file in other folders are byte-identical (same SHA-256) and correctly refused. A 12,437-byte hand-cut
subset under "Sagebrush ETL Test Files" is skipped by design. Deliveries in as-of order: D1 = Jan012025-Feb042026
(as-of 2026-02-04), D2 = 01012024-03062026 (2026-03-06), D3 = 01012026-04052026 (2026-04-05),
D4 = 03012026-06102026 (2026-06-10).

## Verdict

Safe to load as-is: one variant, identical headers, zero quarantine, no corrections, no conflicts, late-file replay
changes nothing, and the presented count (35,671) equals the union of distinct identities. Three findings need
decisions before the vanish rule and any presentation view. (1) The spec's "74 residual duplicates" and "47 exact
duplicates" are not a grain property: all 148 rows are dated 2026-02-04, D1 (pulled that day) carries each once,
and D2/D3 carry each twice, so `(cln_enc_id, order_id, cd_10_clncl_rdr_dgnss_cd)` is unique in every file except
for one day's rows. (2) The report is not one row per order-by-diagnosis: `order_id` functionally determines
patient, chart date and drug in every file, yet one order appears under up to 44 `cln_enc_id` values, and in 2024
and in D4 the (order, encounter) pairs equal orders x encounters per patient exactly. Rows overstate prescriptions
8.2x (35,671 rows, 4,362 order ids). (3) `order_chartdate` should be the event date; no row falls outside its
filename window and no row is dated after its as-of, so the Encounters day-after problem does not occur here.

## Deliveries and as-of

- All CSV, so as-of = filename end date. The `REPORT NAME :` title row carries no date in any file (mdy, ISO and
  8-digit regexes: 0 matches); it is comma-padded to 20 fields in all four. D1-D3 title text is
  "Prescription Report - data", D4 is "Athena Prescription Report - data" (title drift only, header unchanged).
- Max `order chartdate` per file: D1 2026-02-04, D2 2026-03-06, D3 2026-04-04, D4 2026-06-09; min: 2025-01-02,
  2024-03-26, 2026-01-02, 2026-03-02. Rows before filename start 0, after filename end 0, in every file. The rule
  "as-of may not be earlier than the latest event date" passes for all four with no tolerance.
- D2 claims from 2024-01-01 but starts 2024-03-26 (38 rows in March 2024); `coverage_start: 2024-01-01` would
  report Jan-Mar 2024 as a gap.
- Volume by year (union): 2024 21,560 rows / 2,131 order ids / 513 patients; 2025 1,797 / 422 / 155; 2026 to
  Jun 10 12,314 / 1,809 / 548. Jan-Sep 2025 has 9-51 rows a month against 1,230-4,060 in Apr-Dec 2024, then
  Oct 227, Nov 351, Dec 983, Jan 2026 1,858. D1 and D2 agree exactly on 2025, so this is what Athena holds.
- Overlaps (filename windows, identity = all columns + occurrence): D1/D2 window 2025-01-01..2026-02-04: 4,168 vs
  4,256 rows, 0 only in D1, 88 only in D2; D1/D3 window 2026-01-01..02-04: 0 only in D1, 88 only in D3; D2/D3
  window 2026-01-01..03-06: 0 only in D2, 15 only in D3; D2/D4 window 2026-03-01..03-06: 0 only in D2, 99 only in
  D4; D3/D4 window 2026-03-01..04-05: 9 only in D3, 384 only in D4.
- The 88 only-in-D2 rows are all dated 2026-02-01..02-04: 47 are second byte-identical copies of rows D1 has once,
  27 are refill variants of rows D1 has once (see Curated), 14 are new (3 new order ids dated on the as-of day).
- Back-fill is mostly the join growing, not late orders. Of the 375 new (enc, order, icd) rows D4 adds inside D3's
  window, 261 are for order ids D3 already carried (new encounters for existing orders), 11 are new order ids and
  all 11 are dated 2 days before D3's as-of. Lag of the 375 by row date before D3's as-of: 132 within 3 days, 11
  at 4-7, 32 at 8-14, 117 at 15-30, 83 at 31-60. D2->D4 99 new rows, 0 new order ids; D2->D3 15, 0 new order ids.

## Landed

- One variant, 20 columns, header byte-identical across all four files. No extra, missing or prohibited columns.
- Landed line counts equal physical lines minus title and header: 4,168 / 28,018 / 6,982 / 8,168, matching the
  report. CRLF in all four, one trailing line ending, no blank interior lines, 0 cells with leading or trailing
  whitespace. Commas inside quoted fields are common (4,157 of 4,168 D1 lines have more raw commas than the
  header), handled by the CSV reader; pandas and the pipeline both see 20 columns.
- Non-ASCII: 1 cell in `patient firstname` in D3 and 1 in D4 (valid UTF-8; reader default `utf-8-sig`, strict).
- Blanks per file (D1/D2/D3/D4): `quantity prescribed` 359/2,025/515/312; `cd-10 clncl rdr dgnss cd`
  837/2,908/871/679; `no. of refills` 111/462/137/50; `prvdr npi no` 18/518/45/35; provider names similar;
  `sendto provdr` 11/414/15/0; `patient homephone` 0/250/0/0. None is required, so no quarantine.
- `prvdr npi no` is 10 digits wherever present. `order id` is 1-6 digits (1-5 in 2024 only), `cln enc id` 1-5,
  `patientid` 1-5, none with a leading zero. `order type grp` has 1 distinct value in every file.

## Typed

- 0 rows quarantined in any file; row accounting balances; no footer, none expected. The four required columns
  (`order_chartdate`, `cln_enc_id`, `patientid`, `order_id`) have 0 blanks everywhere.
- Both date columns are `%m/%d/%Y`, zero-padded, in every row of every file (ISO 0, unpadded 0). Orientation
  proven: 2,810 / 17,136 / 4,279 / 4,818 chart dates have a day above 12 and all parse. `patientdob` years
  1941-2006. The `%Y-%m-%d` fallback has never been exercised.
- No NDC column exists in this source; `order name` has 0 NDC-shaped tokens (12-55 distinct drug names per file).
  ICD-10 codes are 3-6 characters, letter plus digits, no decimal point, 16-39 distinct per file.
- `no. of refills` is an integer wherever present (0 non-integer values); `quantity prescribed` is free text
  (234 of 3,809 D1 values are bare integers), so `text` is right for it. No spec change is needed to type any row.

## Curated

- Corrected = 0, conflict = 0, late = 0 in every delivery, as expected with all columns as identity. Late-file
  replay of D1: unchanged 4,168, current rows changed 0.
- Byte-identical groups: D1 0, D2 47 (94 rows), D3 47, D4 0; max occurrence 2; the same 47 identities in D2 and
  D3; 0 further groups after strip and case-fold. So 47 is still right, but every one is dated 2026-02-04 and
  D1 carries each of the 47 exactly once. They are never adjacent in file order (0 of 47 with index gap 1).
- The other 27 residual groups under (enc, order, icd) differ in `no. of refills` only: one row blank, one with a
  value, blank first in file order, in both D2 and D3; D1 carries the blank version only. All 54 rows dated
  2026-02-04. The 74 groups touch 7 order ids, 45 encounter ids and 6 patients.
- Functional dependencies (0 violations in every file): `order id` -> `patientid`, `order chartdate`,
  `order name`, `order desc`, `quantity prescribed`, `order type grp`; `patientid` -> names, dob, email, phone;
  NPI -> provider names; `order name` -> `order desc`. Varying within an order id: `cln enc id` (534 / 2,819 /
  931 / 1,053 orders), provider columns (381 / 1,941 / 622 / 667), ICD (5 / 829 / 5 / 0), refills (0/3/3/0).
- (enc, order) groups have 1 or 2 rows, never more; the 2-row groups with two ICD codes number 5,809 in 2024,
  0 in 2025, 47 in 2026. D4 has none: (cln enc id, order id) is unique in D4 (8,168 of 8,168).
- Join shape: per patient, (order, enc) pairs equal orders x encounters in 2024 (15,751 = 15,751), 0.986-0.991
  in 2026, 0.757 in 2025; in D4 483 of 483 patients. Encounter sets of two orders of one patient are always nested
  (0 incomparable pairs in any file). One enc id carries order dates up to 690 days apart. `cln enc id` is
  therefore not the encounter the order was written in.
- Cross-file demographics: 2 patients changed first name (247 rows), 1 email (34 rows), 2 phone (131 rows). The
  only overlap effect is 9 D3 rows re-sent by D4 with a new `patient email`; the rest sit outside overlaps.

## Presented

- Current rows 35,671 = union of distinct identities 35,671 (35,624 distinct by content + 47 occurrence-2 rows).
  By delivery that last set them: D2 23,850, D4 5,332, D1 4,168, D3 2,321.
- Vanish simulation (newer delivery retires current rows dated inside its window that it no longer carries):
  D2 0, D3 0, D4 9 (the email corrections); 9 in total under both filename and actual windows; current after
  vanish 35,662. Double counting until the vanish rule: 9 rows (0.03%). The vanish rule cannot remove the 74
  rows of 2026-02-04 that D2 and D3 both carry twice; if the vendor confirms they are spurious, that is 74 more.
- Prescription grain: `order_id` defines one order and is unique per patient, chart date and drug (0 violations).
  Union: 4,362 order ids, 29,732 (order, enc) pairs, 35,588 (order, enc, icd), 35,671 rows. A view that counts
  rows overstates prescriptions 8.2x; one that counts (order, enc) overstates 6.8x. Rows per order id reach 44.

## Proposed event-date column

`order_chartdate`. Required, never blank, parses in every row, functionally determined by `order_id`, and the
column the deliveries are cut on (0 rows outside any filename window, latest row <= as-of in all four). Window =
filename start..end. `patientdob` is the only other date and is not an event. Caveat: rows for a given chart date
keep arriving in later deliveries as the patient has new encounters (375 new rows inside D3's window from D4), so
"vanished" stays rare but "inserted" for old dates is normal and completeness should be judged in order ids.

## Open questions for the developer

1. Declare `order_chartdate` as the event date for the vanish rule? (yes/no)
2. Rewrite the spec `notes`: the 47 exact and 27 refill-variant groups are all dated 2026-02-04, D1 carries each
   once, and (cln_enc_id, order_id, icd10) is otherwise unique in all four files? (yes/no)
3. Adopt (cln_enc_id, order_id, cd_10_clncl_rdr_dgnss_cd) as `natural_key` with the 2026-02-04 pairs numbered as
   occurrences 1 and 2 (same outcome as today), which turns the 9 email rows into corrections instead of
   retire-and-insert, or keep keyless? (named decision)
4. Ask Athena whether `cln enc id` is the ordering encounter or every encounter of the patient (2024 pairs =
   orders x encounters exactly), and why 7 orders of 2026-02-04 are emitted twice from 03-06 onwards? (yes/no)
5. Presentation grain for prescriptions: `order_id` (4,362) or (`order_id`, `cln_enc_id`) (29,732)? (named decision)
6. Move `coverage_start` to 2024-03-26, or confirm no orders existed Jan-Mar 2024? (named decision)
7. Confirm with the business that Jan-Sep 2025 (9-51 rows a month) is real, not a missing extract? (yes/no)
8. Tag `cln_enc_id` and `order_id` as `pii: join_key` (they join to Encounters once Athena adds the id)? (yes/no)
9. Type `no_of_refills` as integer (0 non-integer values in 46,000+ rows)? (yes/no)
10. Should completeness count order ids per date rather than rows, given rows for a date grow for 60+ days? (yes/no)

## Probes run

- ehr.athena.prescriptions_probe1.py: per file SHA-256, bytes, line endings, non-ASCII bytes, title row shape and
  date search, header list, physical vs pandas row counts, blanks and whitespace per column, date-format counts,
  min/max/yearly/monthly chart dates, dob range, byte-identical rows (raw and folded), eight candidate-key
  duplicate counts, id/NPI/ICD/quantity/refill/email/phone shapes, quote and comma counts.
- ehr.athena.prescriptions_probe2.py: monthly counts, dating and cross-file identity of occurrence-2 rows,
  columns differing within residual key-dup groups, order grain (group sizes, rows vs distinct ICD), functional
  dependencies within and across files, pairwise overlap asymmetry with sub-key matching, vanish simulation under
  filename and actual windows, union counts.
- ehr.athena.prescriptions_probe3.py: exact dates of the 74 groups and D1's view of them, FDs from order id,
  per-patient cross-join test, per-year grain, columns changed in the 9 re-sent D3 rows, back-fill lag buckets,
  rows touched by demographic changes.
- ehr.athena.prescriptions_probe4.py: back-fill split by known/new order id and enc id, enc-set nesting between
  orders of one patient, refill-variant blank-vs-value and file order, lag of new order ids, per-year join ratio.
