# Review: ehr.athena.eligibility

Reviewed 2026-10-15 against `docs/layer-walk/ehr.athena.eligibility.md` and `specs/ehr.athena.eligibility.yaml`.
Counts, column names, file names and reason codes only; no cell values.

## Verdict

Safe to load the three "Report as of" deliveries (03062026, 04052026, 06102026) as-is: the header is
identical in all three, every row types cleanly (0 quarantined), patientid is unique in every file,
the filename as-of agrees with the latest event date inside each file, and every "corrected" row
is a real change (new qualifying-event date, department or demographic fix), not formatting noise.
The late-file replay leaves current rows untouched. Two things must be decided before loading
more: (1) the file `Athena_Patient Eligibility_Feb042026.csv` is a different report definition
(title row lacks "Active", 326 patients absent from every later snapshot, one event date after its
own as-of) and should not be loaded as a delivery of this source without a decision; (2) the
`vanished` outcome is not simulated because the spec declares no event-date column, and for a
snapshot source the right event date is the delivery as-of itself, not a row column.

## Deliveries and as-of

- 5 files found, 4 distinct contents. #4 and #5 (03062026) are byte-identical copies in two folders
  (`Original Files/Sagebrush ETL files 03062026` and `Sagebrush ETL Files 03062026/...`); the walk
  refuses the second as a duplicate, correctly.
- As-of from filename: 2026-03-06, 2026-04-05, 2026-06-10. The "REPORT NAME :" title row carries
  no date in any file, so it cannot corroborate; the data can. Max `ptnt dt f ldst qlfyng vnt` is
  exactly the as-of in the 03062026 and 06102026 files and 2026-04-03 in the 04052026 file; max
  `ptnt dt f nwst qlfyng vnt` is one day before as-of in each. No event date exceeds its as-of in
  any of the three. The filename dates are right.
- `Athena_Patient Eligibility_Feb042026.csv` (23,098 rows) gets no as-of: `coverage_from_name`
  handles `MonDDYYYY-MonDDYYYY` ranges and `as of MMDDYYYY` but not a single `MonDDYYYY` token. Not a
  fault in layer_walk (the rule refuses rather than guesses); the parser could learn the shape,
  but see below: this file should probably not be loaded at all. Its newest-event max is
  2026-04-21, after 2026-02-04 (1 row), so it would also fail the as-of floor rule.
- The title row differs: the Feb file is "Newest/Oldest QE w/ Validation"; the three others are
  "Active Newest/Oldest QE w/ Validation". The Feb file has 326 patients that appear in no later
  snapshot (0 of them reappear in Apr or Jun) and 8 / 6 blank department cells (0 in the others):
  it is the unfiltered report, the others the active-patient report.
- Snapshot semantics: each file restates every active patient. Patients absent from a newer
  snapshot: 2 (Mar to Apr), 1 (Apr to Jun), 3 total absent from the newest. None reappear. The
  vanish rule should retire them (supersede, keep history), because absence from an "Active"
  snapshot means the patient is no longer active in Athena. The event date for coverage is the
  as-of (see "Proposed event-date column").
- Line endings: 03062026 and 04052026 are CRLF with a comma-padded title row; 06102026 is LF-only
  with an unpadded title row. Different export path or re-save; no effect on content (0 rows differ
  by whitespace or case between any pair).

## Landed

- Header identical in all four files, 10 columns, all claimed by the spec. No extra, no missing
  optional, no prohibited columns. Every data line has exactly 10 fields in every file.
- Landed line count equals physical lines minus 2 (title + header) in all four: 23,098 / 23,188 /
  23,391 / 23,826. `preamble_lines: 1` is right. No footer, no blank lines, no BOM.
- The 3,902-byte hand-cut Feb copy under `Sagebrush ETL Test Files` is skipped correctly; the
  full-size Feb file sits at the root of the share, not under the "5 Athena" group folder.

## Typed

- 0 quarantined in every delivery; expected. All three date columns are 100% `mm/dd/yyyy` where
  populated (0 ISO, 0 single-digit, 0 other); `patientid` is never empty, all digits, 1-5 chars,
  no surrounding whitespace. The second format `%Y-%m-%d` is never exercised; harmless.
- Empties are legitimate nulls, not errors: `ptnt vldtn nwst qlfyng vnt` is empty in 99.7% of rows
  (71 populated of 23,188), `ptnt vldtn ldst qlfyng vnt` 81 populated; `ptnt dt f nwst qlfyng vnt`
  empty in 48%, `ptnt dt f ldst qlfyng vnt` in 43%. In 06102026, 9,883 rows have no event date in
  either column, 1,491 have an oldest date but no newest, 225 a newest but no oldest.
- No spec change is needed for quarantine. One optional tightening: `patientdob` could be
  `required: true` (never empty in 93,503 rows) so a blank DOB quarantines rather than lands null.
- Data-quality flag, not a typing error: 56 rows have a newest event date earlier than the oldest
  event date, the same 56 patients in all three active snapshots. Typing cannot catch this; a
  curated check could.

## Curated

- patientid unique in every delivery (23,098 / 23,188 / 23,391 / 23,826 distinct = row count).
  The spec comment "unique across all 23,188 rows" is now verified on four files. 0 within-file
  duplicate identities, max occurrence 1.
- Corrections, columns differing (counts of rows):
  - 03062026 to 04052026, 161 corrected: `ptnt dt f nwst qlfyng vnt` 147, `ptnt dt f ldst qlfyng
    vnt` 14, `curr dprtmnt` 11, `reg dprtmnt` 1, `patient firstname` 1, `patient lastname` 1.
    135 rows changed only the newest date; 123 moved later, 24 filled from empty, 0 moved earlier.
  - 04052026 to 06102026, 517 corrected: `ptnt dt f nwst qlfyng vnt` 500, `ptnt dt f ldst qlfyng
    vnt` 28, `curr dprtmnt` 6, `patientdob` 5, `patient firstname` 2, `patient lastname` 2,
    `ptnt vldtn nwst qlfyng vnt` 1, `ptnt vldtn ldst qlfyng vnt` 1. 477 rows changed only the
    newest date; 434 later, 66 filled, 0 earlier. The 5 DOB changes all move earlier.
  - These match the walk exactly (161 and 517), so the row hash sees precisely the raw changes.
- Real changes, not noise: after strip/casefold/whitespace-collapse the differing-row counts are
  unchanged (161, 517), and the date columns compared as parsed values differ identically. The
  dominant change is a patient acquiring a newer qualifying event, which is the business event
  this report exists to carry. Name/DOB-only changes (1 and 3 rows) are demographic corrections.
- Late replay of 03062026 after the others: late 646, unchanged 22,542, current changed 0. 646 is
  exactly the 03062026 to 06102026 differing-row count, as it should be.

## Presented

- 23,829 current rows = distinct patientids across the three active snapshots (23,826 in the
  newest + 3 patients absent from it, all 3 carrying at least one event date). The count is right
  for the rule as written. With the vanish rule it would be 23,826. If the Feb file were loaded
  with an operator-supplied as-of, current would become 24,155 and the 326 non-active patients
  would stay current forever without a vanish rule.
- Current by last-setting delivery: 03062026 22,542, 04052026 334, 06102026 953; consistent with
  the correction counts.

## Proposed event-date column

For dedup coverage (the `vanished` outcome and the as-of floor), use the delivery as-of itself,
not a row column: this is a full snapshot, so a newer delivery covers every row of an older one,
and 41% of rows have no event date in either column, so no column can define coverage. Suggest a
spec-level declaration such as `event_date: as_of` (snapshot) rather than a column name. For the
as-of floor check, the "latest event date in the file" should be the max over both
`ptnt_dt_f_nwst_qlfyng_vnt` and `ptnt_dt_f_ldst_qlfyng_vnt`; the oldest-date column is the one
that actually touches the as-of. For the business event date used in reconciliation, use
`ptnt_dt_f_nwst_qlfyng_vnt` with fallback to `ptnt_dt_f_ldst_qlfyng_vnt` (1,491 rows have only the
latter), never the newest column alone.

## Open questions for the developer

1. Should `Athena_Patient Eligibility_Feb042026.csv` (non-"Active" report, 326 extra patients) be
   excluded from this source, or loaded as a second variant? Recommend exclude.
2. Should the loader check the "REPORT NAME :" title row text (contains "Active") so a non-active
   export is refused rather than silently loaded as the same variant?
3. Should `coverage_from_name` learn the single `MonDDYYYY` shape, or is an operator-supplied as-of
   the intended path for such names?
4. Confirm the vanish rule should supersede patients absent from a newer active snapshot (3 today).
5. Is `event_date: as_of` (snapshot coverage) acceptable as the spec declaration, instead of a
   column?
6. Should the 56 patients with newest event date before oldest event date be surfaced as a curated
   data-quality flag?
7. Should `patientdob` be `required: true`?
8. PII classes: `patientid` is join_key (clear) as project convention, but it is an MRN; DOB and
   names are direct (encrypted); the two qualifying-event date columns are `none`. Should the event
   dates be `quasi` (dates tied to an individual)?
9. Does the LF-only, unpadded-title 06102026 export indicate a changed export path worth asking the
   finance team about, or a one-off re-save?

## Probes run

- Ran `scripts/layer_walk.py ehr.athena.eligibility --out docs/layer-walk`; it completed.
- Located every file matching the pattern on the share with size and mtime.
- Read `coverage_from_name` in layer_walk.py to see why the Feb filename yields no as-of.
- Printed title row and header row of each file, physical line counts, BOM, CRLF/LF, blank lines,
  trailing newline, and the field-count distribution of data lines.
- Checked what the row hash covers (typed values, spec field list) in `core/identity.py` and
  `ingest/load.py`.
- pandas probe per file: patientid uniqueness, emptiness and digit/length pattern; empty count per
  column; leading/trailing whitespace per column; date-format match counts and min/max/after-as-of
  for the three date columns; newest-before-oldest count; distinct counts of department and
  validation columns.
- pandas probe per consecutive pair: common/absent/new patient counts; per-column and
  column-combination differing counts; differing count after whitespace/case normalisation; parsed
  date direction (later/earlier/filled/emptied); name-or-DOB-only change counts.
- Probe: stability of the newest-before-oldest set across snapshots; event-date presence for the 3
  patients absent from the newest snapshot; rows with no event date in either column.
- Checked `PiiClass` definitions and searched the spec schema and specs for an existing event-date
  field.
