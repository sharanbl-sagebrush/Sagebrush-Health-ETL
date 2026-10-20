# Review: ehr.curve.appointments

Reviewed 2026-10-15 against `docs/layer-walk/ehr.curve.appointments.md` (generated the same day) and
`specs/ehr.curve.appointments.yaml`. Four distinct CSV deliveries, header-identical (101 columns); a fifth file
is a byte-identical copy of the 2023-2026 file in a second folder (SHA-256 equal) and is correctly refused; a
28 KB hand-cut subset under "Sagebrush ETL Test Files" is skipped by design. In as-of order: D1 =
Jan012025-Feb042026 (as-of 2026-02-04, 79,569 rows), D2 = 01012023-03062026 (2026-03-06, 199,591), D3 =
01012026-04052026 (2026-04-05, 19,159), D4 = 03012026-06102026 (2026-06-10, 23,042). Status values are called
S1..S7 in D2 frequency order; S2 equals the count of rows with a cancellation timestamp.

## Verdict

Not loadable as specified. Three of four deliveries (D1, D3, D4: 121,770 rows) quarantine 100% on BAD_DATE
because they write timestamps as 12-hour with an AM/PM suffix while the spec declares only the 24-hour shape that
D2 uses; D2 came through a different export writer (zero-padded 24-hour timestamps without seconds, zero-padded
dates, upper-case booleans). The walk's presented count (199,591) is therefore D2 alone. Two design problems sit
behind the format fault: `type: date` truncates every timestamp to its date, so the typed layer loses the time
of day and the walk reports 292 within-file identity groups in D2 where only 9 byte-identical groups exist; and
with all 101 columns as identity, D2 shares zero content-identical rows with any other delivery in any overlap
window, because seven columns are balance and referral-flag snapshots taken at pull time, not appointment
attributes. After the format fix the ledger would hold 317,804 current rows for about 221,010 distinct
appointments (30.5% double counted) until a vanish rule exists, and every overlapping row would be retired and
re-inserted at each pull. Corrected, conflict and late are 0 and the late-file replay changes nothing.

## Deliveries and as-of

- All CSV, so as-of comes from the filename end date; the order D1 < D2 < D3 < D4 is right. OneDrive
  modification times (2026-02-15, 05-25, 05-25, 07-06) are upload times and are correctly ignored.
  `DateTime_Scheduled` and `DateTime_Cancelled` never exceed the as-of in any file, confirming each pull date.
- `Appointment_DateTime` exceeds the as-of by exactly one day in two files: D1 has 328 rows dated 2026-02-05,
  D4 has 356 rows dated 2026-06-11; D2 and D3 have 0; nothing is later than one day. These rows are mostly S4
  (225 / 252) with some S2 (99 / 95); all 328 of D1's reappear in D2 under (patient, appointment timestamp,
  resource), 229 with a changed status. They are bookings for the day after the pull, and under "as-of may not
  be earlier than the latest event date" D1 and D4 would be refused.
- No row falls before its filename start. D2 claims 2023-01-01 but its first appointment is 2023-03-15
  (777 rows in March 2023, 3,924 in April); Jan-mid Mar 2023 would look like a gap. `DateTime_Scheduled` later
  than the appointment date: 40 / 1,270 / 9 / 8 rows (back-entered bookings).
- Event-date column: `appointment_datetime` (see below).

## Landed

- Header byte-identical in all four files; no extra, missing-optional or prohibited columns. UTF-8 BOM and CRLF
  in all four. Non-ASCII bytes: 49 / 390 / 7 / 8, in address, email and Comments columns only.
- Landed count equals CSV records, not physical lines: non-blank lines minus header exceed records by 37 / 96 /
  5 / 3 because of embedded newlines in quoted cells (Street1, Guar_Street1, Certificate_Number, Comments;
  D2 also Patient_Email, Guar_Email). The reader opens with `newline=""`, so this is handled.
- Whitespace: `State` and `Guar_State` carry trailing whitespace on nearly every row (79,341 of 79,569 in D1);
  12-22 columns per file have it (D2 also the three balance columns, 168-185k each); leading whitespace in the
  provider-name columns, Certificate_Number and Comments. `_as_text` strips at typing; landed keeps it as sent.
- 19 columns are blank in every row of every delivery: Prim_Emp_*, Sec_Emp_*, Emp_*, Visit_Type, RefDocOrg
  and all eleven CM_*. `DateTime_Bumped` is NOT empty, contrary to the spec comment: 1 / 9 / 30 / 33 cells.

## Typed

- Quarantine: every row of D1, D3, D4; 0 rows of D2. Reasons are all BAD_DATE on the six timestamp columns:
  appointment_datetime and datetime_scheduled fail on every row (121,770 each), datetime_acknowledged 66,502,
  datetime_cancelled 49,472, datetime_confirmed 28,612, datetime_bumped 64 (every non-blank cell in those files).
- Cause: D1/D3/D4 write `M/D/YYYY h:mm AM|PM` (DateTime_Confirmed with seconds, `h:mm:ss AM|PM`); D2 writes
  `MM/DD/YYYY HH:MM` 24-hour, zero-padded, no seconds. The spec's `%H:%M` formats parse only D2. Adding
  `%m/%d/%Y %I:%M %p` and `%m/%d/%Y %I:%M:%S %p` to the six columns parses every cell (0 unparsed in a probe).
  Plain dates already parse in both writers because strptime accepts non-padded `%m/%d`.
- `type: date` drops the time of day. There is no datetime type in the vocabulary, so six timestamp columns
  land as dates and the identity hash collapses same-day rows: D2 shows 292 groups / 641 rows / max occurrence 8
  in the walk versus 9 / 19 / 3 in the file.
- `Referral_Is_Required` has two values in every file; D2 writes them upper-case (199,591 rows), the others
  title-case. Typed as text the case difference survives into identity; `parse_boolean` lowercases, so
  `type: boolean` would remove it. `Referral_Status` likewise has 2 values.
- Check_In_Time / Check_Out_Time: all numeric, 2-4 digits; 1 Check_Out value in D2 is not a valid HHMM. Money
  columns (text) carry `$9.99`, `$9,999.99` and parenthesised negatives (1,319 in D3), shapes `parse_currency`
  handles.

## Curated

- Corrected = 0 in every delivery and stays 0 after the format fix (identity equals content by construction).
  Conflict = 0, late = 0.
- Byte-identical rows (stripped cells, full timestamps): D1 6 groups / 13 rows, D2 9 / 19, D3 0, D4 1 / 2; max
  occurrence 3. The notes' "ten" is right as the surplus row count in D2 (19 rows in 9 groups).
- Overlap windows, identity = all columns after strip and timestamp normalisation: D1/D2 (2025-01-01..2026-02-04)
  79,241 vs 79,242 rows, 0 in common; D2/D3 13,497 vs 13,497, 0 in common; D2/D4 1,623 vs 1,623, 0 in common;
  D1/D3 7,241 vs 7,242, 1,845 in common; D3/D4 7,285 vs 7,286, 1,681 in common. In every pair all but one row
  matches the other file on (Patient_Number, Appointment_DateTime, ResDesc).
- What differs on those matches (D1/D2): Referral_Is_Required 77,004 (case), Net_Balance 29,360,
  Insurance_Balance 26,672, Registration_Last_Modified_By 12,080, SelfPayBalance 10,270, Certificate_Number
  7,524, carrier and address columns in the low thousands. Excluding the 7 snapshot columns leaves 13,520
  only-in-D1; also excluding 20 insurance columns 3,201; also demographics 488; on the 27 appointment-only
  columns 4 / 25 / 58 / 6 / 22 per pair: the genuine status, cancellation, check-out and bump changes.
- Patient_Number determines DOB, names, gender and address within every file (0 violations); across deliveries
  315 patients change last name, 290 first name, 4 DOB, 554 street, 2,355 certificate number.
- Simulation after the format fix: D1 inserted 79,569; D2 inserted 199,591; D3 inserted 17,283 / unchanged
  1,876; D4 inserted 21,361 / unchanged 1,681; current 317,804. Late replay of D1: unchanged 79,569, late 0,
  current rows changed 0. Identical under the walk's date-truncated identity (0 spurious cross-file matches).
- Vanish simulation (newer delivery retires older current rows dated inside its window that it did not carry):
  D2 retires all 79,569 D1 rows, D3 retires 13,497 D2 rows, D4 retires 5,604 D3 rows; 98,670 total, final
  221,010, the same under filename and actual windows. That equals the union of distinct (patient, appointment
  timestamp, resource, occurrence) across the four files, 221,010.

## Presented

- Walk as it stands: 199,591 current rows, all from D2, because the other three files quarantine entirely.
- After the format fix: 317,804 current rows versus about 221,010 distinct appointments (221,323 under the
  27 appointment-only columns), so 96,794 rows (30.5% of current, 43.8% over the true count) are the same
  appointment carried by two or three deliveries; D1 inside D2 alone is 79,241 rows. Even with a vanish rule,
  all-column identity records 98,670 retire-and-insert pairs that are balance and case changes, not
  appointment changes; appointment-level change is about 115 rows across the five overlaps.

## Proposed event-date column

`appointment_datetime` (its date part). It is never blank in 321,361 rows, is the column the filename window is
cut on (0 rows before any start), and the vendor's own later-stamped columns never exceed the as-of. Window =
filename start..end. Caveats: rows dated as-of + 1 day exist in D1 (328) and D4 (356), so the as-of rule needs a
one-day tolerance or an exclusion of not-yet-seen statuses, otherwise both files are refused. `date_of_account`,
`patient_date_registered` and the referral dates are not event dates.

## Open questions for the developer

1. Add `%m/%d/%Y %I:%M %p` and `%m/%d/%Y %I:%M:%S %p` to the six timestamp columns (appointment_datetime,
   datetime_scheduled, datetime_confirmed, datetime_cancelled, datetime_acknowledged, datetime_bumped)? (yes/no)
2. Timestamps: add a datetime type to the vocabulary, keep `date` and accept losing the time of day, or type the
   six columns as text? (named decision)
3. Identity for this keyless source: all 101 columns, a declared identity subset (the 27 appointment columns),
   or a natural key (patient_number, appointment_datetime, resdesc) with occurrence (9,076 / 2,240 / 591 / 836
   colliding rows per file become occurrences)? (named decision)
4. Declare `appointment_datetime` as the event date for the vanish rule? (yes/no)
5. As-of validation for the as-of + 1 day rows: one-day tolerance, exclude S4-type statuses, or operator
   as-of for D1 and D4? (named decision)
6. Type `referral_is_required` (and `referral_status` if its vocabulary is boolean) as `boolean` so the two
   writers' case difference disappears? (yes/no)
7. Type the five balance/copay columns as `currency`, or keep text because they are pull-time snapshots, not
   appointment facts? (named decision)
8. Classify `certificate_number`, `sec_certificate_number1`, `authorization_number` and `comments` as
   `pii: direct` (Comments has 212 / 643 / 6 / 9 cells with "@" and 31 / 111 / 20 / 10 with 7+ digit runs)?
   (yes/no) `guar_account_number` and `patient_email` are already direct; staff-name columns stay none.
9. Set D2's coverage start to 2023-03-15, or confirm Curve has no appointments before it? (named decision)
10. Fix the spec comments: `datetime_bumped` is populated (73 cells across files), "ten exact duplicates" are
    10 surplus rows in 9 groups, and list the 19 always-blank columns? (yes/no)
11. Extend the vendor ask beyond an appointment id to a single export writer, given D2's timestamp, date-padding
    and boolean-case differences from D1/D3/D4, and act on `FileLoad.quarantine_rate` so a 100% quarantine
    refuses the file? (yes/no)

## Probes run

- probe1: per file BOM, line endings, non-blank lines vs records, blanks and all-blank columns, digit-masked
  shapes of 15 date/time columns, whitespace cells, byte-identical groups, six candidate-key duplicate counts,
  distinct counts; header equality across files.
- probe2: letter/digit masks confirming AM/PM; spec-format parse failures per column; embedded-newline,
  whitespace and non-ASCII counts by column; appointment-date range, rows before start and after as-of
  (+0/+7/+30 days); scheduled/cancelled maxima; status mix; D2 identity groups with and without time truncation.
- probe3: pairwise overlap asymmetry with sub-key explanation and differing-column counts; curate simulation
  under full and date-truncated identity with late replay; vanish simulation under filename and actual windows;
  union counts; patient functional dependencies; 4/8/10-column keys; free-text and HHMM checks; SHA-256 of the
  two D2 copies.
- probe4: vocabulary sizes per file; five identity tiers (all, minus snapshot, minus insurance, minus
  demographics, appointment-only) with overlap matching and union counts.
- probe5: case-folded vocabulary and overlap matching; D1 post-as-of rows found in D2; D2 early-2023 months.
