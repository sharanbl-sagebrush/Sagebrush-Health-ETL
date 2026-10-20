# Review: ehr.allscripts.erx

Reviewed 2026-10-15 against `docs/layer-walk/ehr.allscripts.erx.md` (generated the same day) and
`specs/ehr.allscripts.erx.yaml`. Four distinct deliveries, all CSV; a fifth file is a byte-identical copy of the
2023-2026 history file in a second folder and is correctly refused (same SHA-256); a 9,696-byte hand-cut subset
under "Sagebrush ETL Test Files" is skipped by design. In as-of order: D1 = Jan012025-Feb042026 (as-of 2026-02-04,
in the root folder), D2 = 01012023-03062026 (2026-03-06), D3 = 01012026-04052026 (2026-04-05), D4 = 03012026-06072026 (2026-06-07).

## Verdict

Loadable as-is through landed, typed and curated, with one delivery the as-of rule will refuse and two decisions
needed before the vanish rule is built. Headers are byte-identical across all four files, zero rows quarantine,
corrected and conflict are 0 everywhere, the late-file replay changes nothing, and the presented count (149,601)
equals the union of distinct (row, occurrence) across the deliveries. The problems: (1) D4 carries 783 rows dated
2026-06-08..06-10, past its filename end date, so its filename as-of (06-07) is wrong and the rule "as-of may not
be earlier than the latest event date" refuses it; its folder is named June102026. (2) All-columns identity makes
demographic drift look like prescription churn: 1,568 of the 1,622 rows the vanish rule would retire are the same
prescription re-stated with a changed phone, email, name, DOB or provider surname. (3) The vanish window must be
the filename window, not the file's actual date range: three files carry a few orders dated before their start
(29 / 15 / 3 rows) and an actual-range window would wrongly retire 27,940 rows. The 8,100 figure is confirmed: it
is the surplus occurrences in D2 (7,548 identity groups, 15,648 rows).

## Deliveries and as-of

- All CSV, so every as-of is the filename end date. D1 and D2 end exactly on their last `ORDERED_DATE`; D3's last
  row is 2026-04-04 (end 04-05); D4's last row is 2026-06-10 against an end of 06-07.
- D4 rows after its filename end: 265 on 06-08, 274 on 06-09, 244 on 06-10 (a normal day's volume: median 196
  per in-window day). None of the 783 appears in any other delivery. The sibling Athena file in the same folder
  ends 06102026. The extraction date is therefore 2026-06-10 and the filename is mistyped; the as-of must be
  supplied or the file renamed, otherwise D4 is refused.
- Rows before the filename start: D1 29 (dated 2023-12 to 2024; all in D2), D3 15 (from 2025-09-02; 6 in D1, 11
  in D2, 3 in D4), D4 3 (all in D3); D2 0. The vendor's filter is not strictly on `ORDERED_DATE`: a small tail of
  older orders rides along, and the same 2025 rows appear in both 2026 files.
- D2 claims from 2023-01-01 but its first row is 2023-03-16 (297 rows in March 2023, 1,990 in April); no
  `coverage_start` is declared, so Jan-Mar 2023 would read as a gap.
- Trailing empty row: D1, D3 and D4 end with one extra CRLF, which `csv` yields as a width-0 last row; D2 has
  none. `skip_trailing_blank_rows` (default on) drops it at end of file, so it is harmless: physical lines minus
  header minus that blank = 58,473 / 132,974 / 14,584 / 16,298, matching landed.
- Event-date column: `ordered_date` (see below).

## Landed

- One variant, 21 columns, header identical in all four files; UTF-8 BOM, CRLF, no embedded newlines, no
  extra, missing or prohibited columns. Landed line counts equal physical data lines (above).
- `LOCATION_NAME1` equals `LOCATION_NAME` in every row of every file (58,473 / 132,974 / 14,584 / 16,298) and
  neither is ever blank. Mapping them as two targets is correct; they are nevertheless fully redundant.
- Blanks: `ICD_10` 15,633 / 35,867 / 3,679 / 3,861 (about 27%); `MED_QUANTITYTEXT` 1,109 / 2,730 / 284 / 319;
  `Pharmacy` 516 / 2,691 / 126 / 147; `ADDR_PHONE` 420 / 3,502 / 65 / 83; `NPI` 20 / 123 / 8 / 6; `Refill`
  9 / 23 / 3 / 0; `DOB` 7 in D2 only (3 patients); `QTY_UNIT` and `ADDR_EMAILADDRESS` small.
- Null-ish placeholders the typer maps to null (`parse_text`): `Refill` 20,746 / 52,484 / 5,266 / 5,919 (35-39%
  of rows), `ADDR_EMAILADDRESS` 542 / 1,257 / 145 / 95, `DEM_LASTNAME` 5 (D2), `ADDR_PHONE` 3 / 3 / 0 / 0.
- Whitespace (trimmed at typing): `ADDR_PHONE` 59 / 92 / 22 / 20, `QTY_UNIT` 48 / 137 / 8 / 8, `DEM_FIRSTNAME`
  2 / 7 / 1 / 0. Non-ASCII: 6 cells in D2 `ADDR_EMAILADDRESS`. One D2 pair differs only in `Pharmacy` case.

## Typed

- 0 rows quarantined in any file; row accounting balances; no footer, none expected. Only `ordered_date` is
  required and it is never blank. Expected.
- Dates: `ORDERED_DATE` and `DOB` are `%m/%d/%Y`, zero-padded, in every row of every file (ISO count 0; the
  fallback has never been exercised). Orientation proven: 34,751 / 79,159 / 8,311 / 8,593 ordered dates have day
  above 12 and all parse. DOB is never after the ordered date; earliest DOB year 1925.
- Spec change worth considering, not made: `refill`. The null-ish mapping turns the vendor's "no refills"
  placeholder (35-39% of rows) into null, indistinguishable from the 9 / 23 / 3 / 0 genuinely blank cells. Change:
  keep the literal text for this column (or declare an explicit value map). Because identity hashes typed
  values, a placeholder row and its blank twin would collapse into one identity; observed count of such
  collapses: 0 in all four files (typed group counts equal raw byte-identical counts).
- Shapes: `NPI` 10 digits wherever present; `IMREENC_CODE` 3-7 digits; `IMREMED_CODE` 2-5 digits (an Allscripts
  internal medication id; the report carries no NDC column at all); `DEM_EXTERNALID` 2-6 digits except 1 (D1) and
  20 (D2) values with letters or punctuation; `QTY` integer in 99% of rows, 284 / 554 / 116 / 129 decimals, one
  10-digit value in D1 and D2 matching no other column of its row (outlier; no control total here); `ICD_10` about
  97% standard dotted codes, 2-3% alphanumeric-tail or unclassified, 0 multi-code separators.

## Curated

- Corrected = 0 in every delivery and structurally impossible here: the identity columns are the same 21 typed
  columns the row hash covers, so equal identity implies equal hash. Conflict = 0, late = 0.
- Byte-identical groups per delivery (groups / rows in them / surplus occurrences / max occurrence): D1 3,075 /
  6,395 / 3,320 / 8; D2 7,548 / 15,648 / 8,100 / 8; D3 534 / 1,110 / 576 / 6; D4 600 / 1,263 / 663 / 6. D2
  multiplicity: 2 x 7,194, 3 x 224, 4 x 96, 5 x 13, 6 x 11, 7 x 7, 8 x 3. Copies are not always adjacent (D2:
  6,366 of 7,548 groups contiguous) and no file is sorted by `ORDERED_DATE`, so occurrence numbering follows raw
  file order as the rule says.
- The 8,100 figure is right and means surplus occurrences in D2 (15,648 - 7,548), i.e. 8,100 rows with
  occurrence 2 or more; "8,100 identities" in CLAUDE.md is true only if identity is read as (columns,
  occurrence). After all four deliveries, current rows with occurrence > 1 number 8,976 (8,702 after vanish).
- Same identical groups with the same multiplicity across overlapping deliveries? Mostly. Within the filename
  overlap window (identities absent from the newer file / present with fewer copies / present with more copies):
  D1->D2 899 (966 rows) / 134 (160 rows) / 31 (32 rows); D1->D3 186 (197) / 41 (47) / 10 (10); D2->D3 251 (263) /
  20 (20) / 12 (12); D2->D4 59 (62) / 8 (8) / 1 (1); D3->D4 194 (203) / 10 (10) / 22 (23). Along the as-of chain
  190 surplus occurrences (160 + 20 + 10) stay current until the vanish rule retires them.
- What the churn is: of the 1,126 D1 rows inside D2's window that D2 does not carry, 1,099 match a D2 row on all
  14 non-PII columns; changed columns `ADDR_PHONE` 372, `ADDR_EMAILADDRESS` 272, `DEM_LASTNAME` 235,
  `DEM_FIRSTNAME` 214, `DOB` 2, across 149 patients. D2->D3: 265 of 283 (one `PROV_LASTNAME` change touches 137
  rows). D3->D4: 204 of 213 (email 96, phone 69, names 45 / 51). Only 28 rows across the chain fail to match the
  newer file even on (`IMREENC_CODE`, `IMREMED_CODE`, `Refill`, `ICD_10`). The report re-states every historical
  prescription with the patient's current demographics, so a phone change retires and re-inserts years of rows;
  the outcome is right (newest wins) but the vanished counter will measure demographic drift.
- Late replay of D1 after everything: 58,473 already current, 0 rows change (walk agrees).
- Flapping under vanish: 12 rows retired by D2 come back in D3 and 7 retired by D3 come back in D4.
- Nothing near a key: duplicates under (`IMREENC_CODE`, `IMREMED_CODE`, `Refill`, `ICD_10`) are 3,808 / 9,388 /
  705 / 834, of which 3,320 / 8,100 / 576 / 663 are byte-identical; dropping `ICD_10` alone raises all-column
  duplicates from 8,100 to 13,095 (grain is order-by-diagnosis plus unexplained exact repeats). The 14 non-PII
  columns give the same duplicate counts as all 21. `DEM_EXTERNALID` determines name and DOB except 3 patients
  in D2; `NPI` determines provider names everywhere.

## Presented

- Current rows 149,601 = union of distinct (row, occurrence) across the four deliveries. By delivery that last
  set them: D1 58,473, D2 75,627, D3 4,538, D4 10,963. Identical to the walk.
- Double counting until the vanish rule exists: 1,622 rows (1.08%). With window = filename range, D2 retires
  1,126 D1 rows, D3 retires 283 D2 rows, D4 retires 213 D3 rows; current after vanish 147,998 (D2 122,657,
  D3 9,043, D4 16,298; D1 holds nothing because D2 covers its whole window). 1,568 of the 1,622 are
  demographic re-statements that have a replacement row in the newer file, so prescription counts are overstated
  by about 1,622, not patient-visible prescriptions lost.
- With window = the file's actual min..max date the simulation retires 27,940 rows (D3's 15 stray 2025 orders
  make it "cover" Sep-Dec 2025 and wipe 17,558 D2 rows), so the window must come from the filename.

## Proposed event-date column

`ordered_date`. It is the only event date in the file (`dob` is the other date), required, never blank, parses
in every row, and the filename windows are cut on it (D1 and D2 end exactly on their last ordered date). Window =
filename start..end. Caveats: (1) D4's latest ordered date is three days after its filename as-of (783 rows), so
the rule refuses D4 until its as-of is supplied as 2026-06-10 or the file is renamed; (2) orders dated before the
filename start (29 / 15 / 3 rows) must insert normally and be left outside that delivery's vanish window;
(3) the vendor's filter is not purely on this column, so a later delivery can carry an older order it did not
carry before (flapping of 12 + 7 rows observed).

## Open questions for the developer

1. Declare `ordered_date` as the event date for the vanish rule? (yes/no)
2. D4 as-of: operator-supplied 2026-06-10 (folder name and last row), rename the file, or refuse it? (decision)
3. Vanish window = filename start..end, never the file's actual date range (1,622 vs 27,940 retirements)? (yes/no)
4. Rows dated before the filename start insert normally and are never retired by that delivery? (yes/no)
5. `refill`: keep the vendor's placeholder as literal text, declare a value map, or accept null? (decision)
6. Identity: keep all 21 columns, or the 14 non-PII columns plus occurrence (same duplicate counts; a
   demographic change becomes a correction of 1 row instead of retire-and-insert of a patient's history)? (decision)
7. Add `coverage_start: 2023-03-16` (first row in D2; 0 rows Jan-Feb 2023)? (yes/no)
8. `location_name1` and `location_name` are identical in every row: keep both targets, or keep one? (decision)
9. Provider names are `pii: direct` here but `pii: none` on Athena encounters (public NPPES data): align? (decision)
10. Confirm the report carries no NDC and extend the vendor ask (order id) to include NDC or RxNorm? (yes/no)
11. Restate "8,100 byte-identical rows" in spec notes and CLAUDE.md as 7,548 groups / 15,648 rows / 8,100 surplus? (yes/no)
12. The lone 10-digit `qty` (1 row, present in D1 and D2): leave as typed, or add a `max` that quarantines? (decision)

## Probes run

- probe1: per file: byte structure and trailing width-0 row, header equality, blanks/whitespace/non-ASCII per column, date formats and day>12, date range vs window, byte-identical groups and multiplicity, shapes, candidate-key and all-minus-one duplicates, functional dependencies, group contiguity.
- probe2: pairwise overlap with (row, occurrence) identity and multiplicity, non-PII and candidate-key matching of older-only rows, out-of-window rows traced across deliveries, dedup replay, union count, vanish under three windows, late replay of D1.
- probe3: odd shapes (ids, 10-digit QTY, ICD_10, phone), LOCATION_NAME1 vs LOCATION_NAME, D2 2023 months, which PII columns differ for re-stated rows, occurrence profile, flapping, D4 post-as-of rows by date.
- probe4: null-ish placeholder cells per column, finer ICD_10 classes, 10-digit QTY against other id columns.
- SHA-256 of the two copies of the 01012023-03062026 file to confirm the refused duplicate.
