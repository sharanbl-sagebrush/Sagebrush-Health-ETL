# Review: ehr.athena.encounters

Reviewed 2026-10-15 against `docs/layer-walk/ehr.athena.encounters.md` (generated the same day) and
`specs/ehr.athena.encounters.yaml`. Four distinct deliveries, all CSV; a fifth file is a byte-identical
copy of the 2024-2026 history file in a second folder and is correctly refused (same SHA-256). A 5,930-byte
hand-cut subset under "Sagebrush ETL Test Files" is skipped by design. Deliveries are referred to in as-of
order: D1 = Jan012025-Feb042026 (as-of 2026-02-04), D2 = 01012024-03062026 (2026-03-06),
D3 = 01012026-04052026 (2026-04-05), D4 = 03012026-06102026 (2026-06-10).

## Verdict

Safe to load as-is, with two decisions needed before the vanish rule is built. Every delivery binds to the one
variant with identical headers, zero rows quarantine, there are no byte-identical rows, the dedup rule produces
no corrections and no conflicts, the late-file replay changes nothing, and the presented count (21,351) equals
the union of distinct rows across the four deliveries. The double counting the vanish rule will remove is only
27 rows (0.13%). The decisions: (1) `cln_enc_date` must be declared the event date, but this report is Athena's
*Appointment* report and carries future-dated scheduled rows, so 2 of 4 files (D1: 4 rows, D4: 11 rows) have an
event date one day after their as-of and would be refused by the rule "as-of may not be earlier than the latest
event date"; (2) a five-column combination is unique in every file, which contradicts the spec's "only all
columns together are unique" and the developer should decide whether it becomes the natural key.

## Deliveries and as-of

- All four are CSV, so as-of comes from the filename end date. The `REPORT NAME :` title row carries no date in
  any file (regex for mdy, ISO and 8-digit shapes: 0 matches), so the filename is the only as-of source.
- Event dates stay within the filename window except for the day after the end: D1 has 4 rows dated 2026-02-05,
  D4 has 11 rows dated 2026-06-11; D2 and D3 have 0. All 15 carry the same rare status (87 rows of 34,840 across
  all files; labelled S4 here), and all 4 of D1's reappear in D2 for the same patient, date, sendto provider and
  NPI under a different status. These are appointments scheduled for the day after the pull, not an as-of error.
- No row in any file falls before its filename start. D2 claims from 2024-01-01 but its first row is 2024-03-26
  (9 rows in March 2024, 482 in April); `coverage_start: 2024-01-01` would make Jan-Mar 2024 look like a gap.
- Overlaps (filename windows, identity = all columns): D1/D2 share 2025-01-01..2026-02-04 with 10,716 and 10,766
  rows, 20 only in D1 and 70 only in D2; D1/D3 share 2026-01-01..02-04: 0 only in D1, 23 only in D3; D2/D3 share
  2026-01-01..03-06: 2 only in D2, 45 only in D3; D2/D4 share 2026-03-01..03-06: 2 only in D2, 16 only in D4;
  D3/D4 share 2026-03-01..04-05: 1 only in D3, 20 only in D4.
- The "only in the newer file" rows are back-filled encounters: D2 carries 70 rows dated Feb-Dec 2025 that D1,
  extracted in Feb 2026, did not have. A delivery is therefore not complete for a date until some months later.
- Event-date column: `cln_enc_date` (see below).

## Landed

- One variant, 11 columns, header byte-identical across all four files. No extra, missing or prohibited columns.
- Landed line counts equal physical data lines: non-blank lines minus title and header = 10,720 / 18,574 / 2,647 /
  2,899, matching the report; each file ends with exactly one trailing newline.
- Title-row drift, not header drift: D1's title reads "Appointment Report - data", is comma-padded to 11 fields
  and the file uses CRLF; D2-D4 read "Athena Appointment Report - data", unpadded, LF. D1 went through a
  different writer (Excel re-save is the likely explanation). Data content is unaffected: 0 cells with
  leading/trailing whitespace, quoting pattern identical, dates zero-padded in all four, 0 patient ids with a
  leading zero. `preamble_lines: 1` handles both shapes.
- Blanks per file: `sendto provdr` 2,725 / 4,789 / 690 / 770; `prvdr npi no` 19 / 278 / 9 / 6; provider names
  similar; all other columns 0 blanks. The spec marks none of these required, so no quarantine.
- Note: the spec comment "Header is on row 1" means the second physical line; the setting is right, the wording
  is zero-based.

## Typed

- 0 rows quarantined in any file; row accounting balances; no footer, none expected.
- Both date columns are `%m/%d/%Y`, zero-padded, in every row of every file (ISO count 0). Orientation is
  proven: 6,972 / 11,472 / 1,605 / 1,669 encounter dates have a day component above 12 and all parse. The
  `%Y-%m-%d` fallback has never been exercised.
- `prvdr npi no` is 10 digits wherever present (10,701 / 18,296 / 2,638 / 2,893). `patientid` is all-digit,
  1-5 characters. No spec change is needed for typing.

## Curated

- Corrected = 0 in every delivery, as expected with all columns as identity. Conflict = 0, late = 0.
- Byte-identical rows: 0 groups in every file (also 0 after strip and case-fold), so the notes' claim holds and
  every occurrence number is 1.
- Outcomes: D1 inserted 10,720; D2 inserted 7,878 / unchanged 10,696; D3 inserted 782 / unchanged 1,865;
  D4 inserted 1,971 / unchanged 928. Late-file replay of D1: unchanged 10,720, current rows changed 0.
- Vanish simulation (newer delivery retires current rows dated inside its window that it no longer carries):
  D2 retires 24 D1 rows, D3 retires 2 D2 rows, D4 retires 1 D3 row; 27 in total, identical whether the window is
  the filename range or the file's actual min..max date. Of the 24: 4 are the future-dated scheduled rows that
  came back under another status, 3 are demographic corrections (same patient, date, provider and status; a
  changed name), the rest are absent from D2 under any status for that patient and date.
- Functional dependencies: within every file, patientid determines lastname, firstname and dob (0 violations
  of 1,590 patients), and NPI determines provider names (0 violations). Across files, 3 patients have a changed
  name (1 lastname, 2 firstname, 0 dob), touching 198 rows. With all columns as identity, a name correction is
  retire-and-insert rather than a correction; with a key it would be a correction.

## Presented

- Current rows 21,351 = union of distinct rows across the four deliveries (21,351). No row is counted twice by
  content.
- Double counting until the vanish rule exists: 27 superseded rows remain current (21,351 vs 21,324), 0.13%. By
  delivery that last set them: D1 10,720, D2 7,878, D4 1,971, D3 782; after vanish D1 would hold 10,696.

## Proposed event-date column

`cln_enc_date`. It is required, never blank, parses in every row, and is the date the deliveries are cut on
(0 rows before any filename start). Window = filename start..end. Caveats: the rule "as-of may not be earlier
than the latest event date" needs a tolerance for this source (rows dated as-of + 1 day exist in 2 of 4 files
and all carry the scheduled status), or those rows must be excluded from the check; otherwise D1 and D4 are
refused. `patientdob` is the only other date and is not an event date.

## Open questions for the developer

1. Declare `cln_enc_date` as the event date for the vanish rule? (yes/no)
2. How should as-of validation treat future-dated scheduled rows: tolerance of 1 day, exclude rows with the
   scheduled status, or require an operator-supplied as-of for D1 and D4? (named decision)
3. The five columns (patientid, cln_enc_date, sendto_provdr, prvdr_npi_no, cln_enc_status) are unique in all
   four files (0 duplicates each; 21,348 distinct across the union vs 21,351 rows, the 3 being name changes) and
   order_dprtmnt has 1 distinct value. Adopt them as `natural_key`, or keep keyless because status is a state,
   not an identity, and without it 449 / 626 / 125 / 188 rows per file collide? (named decision)
4. Update the spec `notes` and the "Candidate-key columns" comment, which tested only four columns and omit NPI
   and status? (yes/no)
5. Move `coverage_start` to 2024-03-26, or confirm no encounters existed Jan-Mar 2024? (named decision)
6. Should the vendor ask for the encounter id also ask why the title says "Appointment Report" when the
   catalogue calls it "Encounter Report", and whether scheduled (not yet seen) rows can be excluded? (yes/no)
7. Keep `pii: none` on `prvdr_npi_no` and provider names (public NPPES data) and no pii tag on `cln_enc_date`,
   consistent with Curve appointment dates? (yes/no) The patient columns are classed correctly: patientid
   join_key, names and dob direct.
8. Should completeness reporting expect back-fill, given D2 added 70 rows to months D1 already covered up to
   12 months earlier? (yes/no)

## Probes run

- probe1: per file, title-row structure and date search, header list, non-blank line count vs pandas row count,
  blanks per column, date-format counts, min/max/yearly encounter dates, byte-identical rows, distinct counts,
  patientid and NPI shape, duplicate counts for eight candidate-key combinations.
- probe2: day>12 orientation check, rows outside filename window, functional dependencies patientid->demographics
  and NPI->names (within and across files), pairwise overlap-window asymmetry with sub-key matching, vanish
  simulation under filename and actual windows.
- probe3: status mix (anonymised labels) overall, of post-as-of rows and of the 27 retired rows; retired rows
  still present under the five-column key; union counts; 2024 monthly counts in D2; blank sendto by status;
  whitespace cells.
- probe4: quote and comma counts, leading-zero ids, non-ASCII byte counts per file; whether D1's future-dated
  rows reappear in D2 under another status.
- SHA-256 of the two copies of the 01012024-03062026 file to confirm the refused duplicate.
