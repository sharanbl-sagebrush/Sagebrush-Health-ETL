# Review: dispense.weinfuse.patients

Reviewed 2026-10-15 against `docs/layer-walk/dispense.weinfuse.patients.md` (generated the same day) and
`specs/dispense.weinfuse.patients.yaml`. Four distinct deliveries; a fifth file is a byte-identical copy of the
2023-2026 history CSV in a second folder and is correctly refused; a 14,902-byte hand-cut subset under
"Sagebrush ETL Test Files" is correctly skipped. Deliveries in as-of order: D1 = Jan012025-Feb052026.csv
(as-of 2026-02-05), D2 = 01012023-03062026.csv (2026-03-06), D3 = 01012026-04052026.csv (2026-04-05),
D4 = 03012026-06102026.xlsx (2026-06-10, from the workbook stamp). D1-D3 bind to `legacy_export` (48 columns),
D4 to `reporting_module_2026` (22 named columns plus one blank).

## Verdict

Loadable but not yet correct across the variant boundary. Every file binds, lands line-for-line, types with
zero quarantine, the key is unique in every delivery, identity is sound, and the late-file replay changes
nothing. Two spec-level faults need a decision before D4-shaped files are allowed to supersede legacy rows:
(1) the new module's `Created Date` is bound to `last_updated`, but it is the record creation date, not a
change stamp: on all 145 patients D4 shares with the legacy files it is never later than the legacy value
(0 of 145) and is earlier on 110, so 110 current rows now carry a `last_updated` that moved backwards;
(2) every one of the 145 cross-variant "corrections" is an artefact of the hash being computed over each
variant's own landed columns (48 vs 22), and each of them nulls at least 6 populated columns on the
patient's current row (`location_id`, `weinfuse_uid`, `first_name`, `last_name`, `sex` and the primary
insurance block on all 145). Only 26 of the 145 differ on any common non-date column. Neither is a reader
defect; both are what the spec says to do today.

## Deliveries and as-of

- As-of is right for all four. D1-D3 are CSV, so as-of is the filename end date; `Jan012025-Feb052026`
  parses. D4's stamp (2026-06-10, Apache POI, no modified stamp) equals its filename end date. No row in any
  file has `last_updated` after its as-of (0, 0, 0, 0), and the newest row is on the as-of day in D1, D2, D4.
- What the legacy filename window selects on is `Last Updated`: rows inside the window are 1,027 of 1,028
  (D1), 1,918 of 1,918 (D2), 215 of 217 (D3); the 1 and 2 remaining rows fall exactly one day before the
  window start, which is a timezone offset between the vendor's filter and the UTC display, not a leak.
  `Date of Birth` is never inside the window, so it is not the filter.
- D4's window cannot be checked from the file: 108 of its 242 rows have `Created Date` before 2026-03-01, so
  the filter is not creation date; the 97 patients new to us were all created inside the window, and 72 of
  the 145 known ones were last updated (per legacy files) on or after 2026-03-01. Consistent with the same
  last-updated filter on a column the new module no longer exports; not provable.
- Event date for a dimension: there is no business event; the only candidate is the change stamp
  `last_updated`. It is incomplete: 120 of the 165 D1-to-D2 corrections change insurance member IDs or
  effective dates with `last_updated` and all three `*_entered_at` unchanged, so insurance edits are not
  stamped. The vanish rule as written is safe but empty here: in all six delivery pairs, 0 patients absent
  from the newer file have `last_updated` inside the newer file's coverage (1,734 of 1,734 D2 patients
  absent from D3 were last updated before 2026-01-01). Absence from a windowed extract means "unchanged in
  the window", never "deleted"; the new module signals that with `status` (2 distinct values, 0 null).

## Landed

- Raw lines after the header equal the report's landed counts in every file: 1,028, 1,918, 217 (all records
  48 cells wide, 0 blank, 0 embedded newlines) and 242 worksheet rows (0 blank). No BOM; D1 is CRLF, D2 and
  D3 LF.
- Header drift between legacy deliveries: none (48 names, same order, in D1, D2, D3). No extra, missing
  optional or prohibited column in any file; no header mentions SSN.
- Variant boundary: the new module lacks 38 legacy headers, among them `Weinfuse UID`, `First Name`,
  `Middle Initial`, `Last Name`, `Sex`, `Ethnicity`, `Race`, `Preferred Language`, `Work Phone`, `Group ID`,
  `Location ID`, `Last Updated` and all 21 insurance-detail columns; it adds `Patient Name`, `Status`,
  `Substatus` (null on 242 of 242), `Created Date`, three `* Insurance Plan` columns and a blank-named
  column at position 0 holding integer row numbers (242 of 242). The spec comment's "23 columns" counts
  that blank one; 22 land.
- The blank header is matched by the junk pattern `^$`, so the column is excluded before landing and is not
  reported in `new_columns`. Harmless here (it is a row counter, as in McKesson), but it is a column dropped
  before landed that the drift report never mentions.

## Typed

- 0 quarantined cells in any delivery; no spec change is needed for parsing.
- Date shapes: D1 ships `Date of Birth` and the three `* Effective Date` columns as US `m/d/Y` (1,028 of
  1,028 DOB); D2 and D3 ship ISO; timestamps are `Y-m-d H:M:S UTC` in all three. The second DOB format is
  right: 624 of D1's 1,028 parsed DOBs have day > 12 (impossible under d/m/Y) and all 1,028 agree exactly
  with D2's ISO value for the same patient; 0 month/day swaps in any effective-date column either.
- Native xlsx datetime cells, confirmed: D4's `Created Date` and `DOB` are 242 of 242 `datetime` cells
  (not text); `parse_date` returns `.date()` from them without consulting `date_formats`. 0 of 484 carry a
  time part, so nothing is lost in this file. CSV timestamps lose their time part too (`strptime(...).date()`),
  so `type: date` drops the time of `last_updated` and `*_entered_at` on all 3,163 legacy rows uniformly.
  Verdict: not a data defect for this source; a guard gap (a wrong declared format cannot stop a native-cell
  workbook) and a model gap (no datetime field type, so intra-day ordering of the change stamp is gone).
- Blank header: confirmed above; harmless, silent.
- `patient_identifier` types as `str` in all four (D4's `Patient ID` cells are text, not numbers), with 0
  leading zeros, so the key's text form is stable across CSV and xlsx.

## Curated

- `patient_identifier` is unique in every delivery (1,028; 1,918; 217; 242 distinct), as is `Weinfuse UID`
  in the three legacy files, and the UID-to-identifier pairing never changes (0 of 1,951). 0 of D4's 97 new
  identifiers match an existing patient on (DOB, normalised name), so the identity is sound and D4 does not
  re-key anyone. 10 (DOB, name) signatures are shared by two legacy identifiers: possible duplicate charts in
  the vendor system, not an ETL issue.
- Legacy-to-legacy corrections are real edits. D1 to D2 (165): `primary_insurance_member_id` 79,
  `last_updated` 45, `primary_insurance_effective_date` 42, `secondary_insurance_effective_date` 39, then
  insurance IDs/names (12 to 6), `address_line_1` 3, `zip_code` 2, single phone/email/name changes. D2 to D3
  (30): `last_updated` 25, `primary_insurance_effective_date` 13, then insurance, email 5, phones, address.
  0 reorderings, 0 within-file duplicate identities, 0 conflicts.
- Cross-variant corrections (D4, 145) are formatting of the hash, not data: 0 of 145 hash-identical because
  the hash fields are each variant's landed targets (48 vs 22), despite load.py's comment that a row from
  either WeInfuse shape hashes identically. On the 15 common non-date columns only 26 rows differ (`email`
  17, `zip_code` 13, `home_phone` 11, `address_line_1` 10, `mobile_phone` 8, `city` 7, `state` 4,
  `address_line_2` 3). `patient_name` equals legacy first+last on 144 of 145. The other 110 "corrections"
  exist solely because `Created Date` was written into `last_updated`.
- Damage to the current row from those 145 supersessions: all 145 lose `group_id`, `location_id`,
  `weinfuse_uid`, `first_name`, `last_name`, `sex`; 144 lose the primary insurance block; 130 `preferred_language`;
  105 `ethnicity`; 73 `race`; 60 the secondary insurance block. `location_id`, the site key, is the one that
  matters for 340B attribution (data-findings 12).
- Late replay of D1: 218 late, 810 unchanged, 0 current rows changed. Correct.

## Presented

- 2,048 current rows equals the distinct identifiers across all four deliveries, so no double counting and
  no lost patient. Composition by last-setting delivery: D2 993, D1 810, D4 242, D3 3.
- But 145 of the 2,048 are the degraded D4 versions above, and 110 of them show a `last_updated` older than
  the version they superseded. A consumer asking "where is this patient seen" gets null `location_id` for
  every patient the new module has touched.

## Proposed event-date column

`last_updated`, legacy variant only, and with a caveat that it should not drive vanish for this source.
Evidence for the column: it is populated on every row, never exceeds the as-of, and the legacy filename
window selects on it (3,160 of 3,163 rows inside, the rest one day early). Evidence against using it to
retire rows: this is a dimension restated by windowed extracts, absence is not deletion, and in every pair
of deliveries the rule would retire 0 rows anyway. The new module has no change stamp at all; its
`Created Date` must bind to a new target (`created_date`, nullable in the legacy variant) and `last_updated`
must be null there, so no D4-shaped row can ever look older or newer than it is. Until then, no event-date
column exists for `reporting_module_2026`.

## Open questions for the developer

1. Rebind `Created Date` to a new target `created_date` and leave `last_updated` null in `reporting_module_2026`: yes/no?
2. Compute the row hash over the union of both variants' targets (nulls included) rather than each variant's landed set, so a legacy row and its new-module twin can be "unchanged": yes/no? If no, name the rule that decides a cross-variant correction.
3. Should a new-module row supersede a legacy row at all when it would null `location_id`, `weinfuse_uid` and names, or should the curated layer coalesce column-wise (latest non-null per column) for dimensions: named decision (supersede / coalesce / hold for review).
4. Turn the vanish rule off for this source (dimension, windowed extracts, `status` carries inactivity): yes/no?
5. Declare `last_updated` as the event-date column for `legacy_export` only, once per-variant declaration exists: yes/no?
6. Add a datetime field type so change stamps keep their time (3,163 legacy rows currently truncated to the day): yes/no?
7. Should native xlsx datetime cells be checked against the declared formats' shape (date vs timestamp) so a format drift still stops a workbook load: yes/no?
8. Report junk-header columns (the blank row-number column) in the landed drift report even though they are dropped: yes/no?
9. `patient_identifier` is an MRN-shaped number (1,897 of 1,918 all digits, 3 to 8 long) kept in clear as a join key; is that the intended classification, or should it land encrypted like the UID-less direct identifiers: named decision.
10. Classify `primary_insurance_group_id` (employer plan group, populated on 382 of 1,918 D2 rows) as `quasi`: yes/no?
11. Ask WeInfuse whether the new report can carry `Last Updated` and `Location ID` (data-findings 12): yes/no?
12. Review the 10 pairs of legacy identifiers that share a date of birth and name as possible duplicate charts: yes/no?

## Probes run

- Parsed each CSV from raw bytes: BOM, line endings, physical lines, record count and widths, blank records, embedded newlines, blank header cells.
- Opened the workbook: core.xml created/modified stamps and application, sheet names, header cells and blank positions, per-column cell types, datetime cells with a non-midnight time part.
- Compared the four header rows for name and order drift; listed headers only in each side of the variant boundary.
- Classified raw cells of every date column by shape (ISO date / ISO timestamp UTC / US / blank) per CSV; characterised `Patient Identifier` and `Weinfuse UID` (digit-only, length distribution, leading zeros, distinct count).
- Loaded every file with the real loader: outcome, variant, quarantine reasons, header matches for the key and date columns, landed target count, Python types of typed dates, min/max of `last_updated` and `date_of_birth` against as-of and coverage, monthly histogram, null counts per column.
- Keyed every delivery by identity hash; for each pair counted shared/only-one-side identities, hash-identical rows, differing common columns, direction of `last_updated` change, rows differing with `last_updated` unchanged, and absent patients inside the newer coverage.
- For D4's known patients compared `Created Date` with the earliest and newest legacy `last_updated`; for its new patients checked creation inside the window; counted `status` distinct values.
- Tested D1-to-D2 effective-date differences for month/day transposition and null direction; checked whether `*_entered_at` moves on unstamped edits.
- Replayed D1-D3 through the real `curate`, then measured each D4 correction against the prior current row: `last_updated` direction, populated columns lost, genuine differences on common columns, `patient_name` vs first+last.
- Cross-checked D4 new and all legacy identifiers on (DOB, normalised name); checked UID-to-identifier stability; counted rows one day before coverage start and on the as-of day per legacy file; counted distinct identities across all deliveries.
