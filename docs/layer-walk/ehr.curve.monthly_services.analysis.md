# Review: ehr.curve.monthly_services

Reviewed 2026-10-15 against `docs/layer-walk/ehr.curve.monthly_services.md` (generated the same day) and
`specs/ehr.curve.monthly_services.yaml`. Four distinct CSV deliveries (UTF-8 BOM, CRLF); a fifth file is a
byte-identical copy of the history file in a second folder and is correctly refused; the hand-cut subset under
"Sagebrush ETL Test Files" is skipped by design. In as-of order: D1 = Jan012025-Feb042026 (2026-02-04),
D2 = 01012023-03062026 (2026-03-06), D3 = 01012026-04052026 (2026-04-05), D4 = 03012026-06102026 (2026-06-10).
SSN sits at header position 18 in every file; the walk matched and dropped it, and no probe here read it
(every probe whitelists columns with `usecols`).

## Verdict

Loads cleanly, but the curated and presented layers are wrong by construction until one of two decisions is
taken. Every file binds, zero rows quarantine, all ten date columns parse, there are no byte-identical rows,
and the deliveries are exact snapshots of each other on their shared posting dates. But the spec's "no natural
key" finding does not hold: `(Service_ID, VoidSign)` is unique in all four files (0 duplicates), and the 5,803
"duplicates" are the two halves of void reversals. Because identity is every column and state columns
(TotPmt, TotAdj, BillDate, Carrier, insurance ids) move between deliveries, no re-delivered line ever matches
its earlier self, so the presented layer holds 350,908 rows where 250,643 distinct charge lines exist:
100,265 rows (28.6%) and $85,324,733.48 of Fee (44.7%) are double counted. Either adopt the key or declare
`postdate` the event date so the vanish rule can retire the stale copies; the key is the better fix, because
the vanish rule would retire-and-insert every re-delivered line instead of recording a correction.

## Deliveries and as-of

- All CSV, so as-of comes from the filename end date; there is no title row or embedded stamp. `PostDate` stays inside the filename window in every file (0 rows before the start, 0 after the end) and its
  maximum never exceeds the as-of (D1 2026-02-04, D2 2026-03-06, D3 2026-04-04, D4 2026-06-09). The files
  are cut on posting date. `SvcDate` is not the cut: 1,104 / 1 / 1,260 / 1,193 rows precede the window start
  (earliest 2005-12-18 in D4) and `PostDate >= SvcDate` in every row of every file.
- `PIF_4` is future-dated (D1: 28 rows in 2027, after its as-of): an expiry-type date the as-of floor must skip.
- D2 claims 2023-01-01 but its first posting is 2023-03-29 (first BillDate 2023-03-01); Jan-Mar 2023 is empty.
- Overlaps, identity = `(Service_ID, VoidSign)`, window = shared PostDate range: D1/D2 81,759 vs 81,759 rows,
  D1/D3 6,748 vs 6,748, D2/D3 13,281 vs 13,281, D2/D4 1,541 vs 1,541, D3/D4 7,186 vs 7,186; in every pair 0
  rows exist on only one side and the window Fee sums agree to the cent. There is no back-fill: a delivery is
  complete for a posting date on the day it is cut. Fee, Units, SvcDate, PostDate and ProcCode never change
  for a line across deliveries (0 identities with more than one value).
- Event-date column: `postdate` (see below).

## Landed

- Header drift: D1 has 118 columns and lacks `PatStreet1`, `PatStreet2`, `PatCity`, `PatState` (optional, so
  it loaded); D2-D4 have 122, byte-identical headers. Every file carries 8 `Textbox*` artifact columns
  (108, 101, 78, 53, 38, 143, 99, 90); the spec `notes` say five, though the column map marks all 8 positions.
- Landed counts equal physical data rows: 81,759 / 230,192 / 18,926 / 21,992 by the csv module. Raw newline
  counts are higher (81,794 / 230,284 / 18,939 / 21,998) because 20 / 58 / 9 / 2 rows carry a line break
  inside a quoted cell. D3 and D4 end with one fully blank record after the last data row, dropped by the
  default `skip_trailing_blank_rows`; D1 and D2 have none. Every row has the full column count.
- Two writers. D1 and D2: zero-padded dates, money with a trailing space, integer Units, `PostMonth` as
  mon-yy, negatives in `Encounter` and `WorkRVU` with a leading minus, `IsVoided` upper-case. D3 and D4:
  unpadded dates (except `BillDateInit`, `PIF_4`, `PIF_5`), no trailing space, Units with two decimals,
  `PostMonth` as mon yyyy, parenthesised negatives in `Encounter` (80 / 79 rows) and `WorkRVU` (126 / 106),
  `IsVoided` title-case, different label text in `PIF_1`. Typing normalises dates and money but not text,
  so no D1/D2 row can ever hash-match a D3/D4 row under all-columns identity.
- `VisitLocAbbr` has trailing spaces in 21,129 / 63,107 / 4,861 / 5,724 cells; `parse_text` trims. Required
  `Voucher_Number` and `Service_ID` are never blank. Blanks elsewhere: `BillDate` 11,601 / 23,655 / 2,388 / 2,779,
  `BillDateOrig` and `BillDateInit` up to 1,725, `EncounterNum` 10,461 / 57,396 / 2,512 / 2,713, `PIF_4` 85-88%
  and `PIF_5` over 99% of rows; D2 has 1 blank `DOB` and 15 blank `ClaimNum`.

## Typed

- 0 rows quarantined in any file; accounting balances; no footer, none expected. All ten date columns parse under `%m/%d/%Y` in every non-blank cell of every file (0 failures in 40
  column-file pairs, 0 ISO values anywhere, so the `%Y-%m-%d` fallback is unexercised). Orientation is proven
  by day-above-12 counts (PostDate 53,691 / 150,219 / 11,788 / 12,719). `PIF_4` (12,339 / 43,638 / 2,326 /
  2,568 values) and `PIF_5` (259 / 1,548 / 25 / 43) are real dates, padded in every writer.
- `Fee` is `$x,xxx.xx` with parenthesised negatives (919 / 5,775 / 165 / 138 rows, 0 leading-minus) and
  `parse_currency` handles both the parentheses and the old writer's trailing space; probe sums equal the
  walk's control totals. `WorkRVU` (decimal) mixes leading-minus and parenthesised negatives; `parse_decimal`
  accepts balanced parentheses. `Units` has 1 non-integer value (D2); negative Units equal VoidSign<0 rows.
- Spec changes that would improve typing, named not made: `totpmt`, `totadj`, `totref` are money (`$`,
  parentheses, 169 / 156 / 844 negatives in D2) but typed `text`; `isvoided` is a two-valued flag typed `text`
  so the writers' case drift survives (`boolean` in `finance.monthly_payments`); `voidsign` is exactly 1 or -1
  typed `decimal` (`integer` in payments); `encounter` has 3 values with two negative notations, typed `text`.
  None cause quarantine today; they decide whether the two writers' values compare equal.

## Curated

- Corrected = 0 in every delivery, as all-columns identity guarantees; conflict = 0; late = 0. Byte-identical
  rows: 0 groups in every file, also 0 after strip and case-fold; occurrence is always 1.
- Key: `(Service_ID, VoidSign)` has 0 duplicates in all four files; so does `(Voucher_Number, Service_ID,
  VoidSign)`. Every voided line is a pair: 5,803 D2 Service_IDs appear twice, with VoidSign +1 and -1, Fee sum
  0 and Units sum 0 in 5,803 of 5,803, both rows flagged IsVoided; no Service_ID has more than 2 rows in any
  file. Voucher never varies within a file for a Service_ID but changes between deliveries for 1 / 3 / 4 / 1
  lines per overlap pair, so it is state, not identity. When the reversal posts in a later window the flagged
  original stands alone (D1 272, D3 122, D4 87 such rows; D2 0), which is why IsVoided differs for 53 lines
  between D1 and D2.
- Same line, later delivery: among the 81,759 D1/D2 matches the columns that moved are `BillDate` 5,811,
  `TotPmt` 4,844, `TotAdj` 4,486, `CurInsCertNum` 3,218, `Carrier` 2,841, `InsCat` 2,814, `CurCarPayorNum1`
  2,750, `PriCarrier` 2,365, `PIF_4` 2,338, `BillMedia` 1,969, `PatZipCode` 772, `DOB` 19, `IsVoided` 53. The
  report is a charge ledger whose rows carry running state; every other overlap pair shows the same pattern.
- So D2's 230,192 inserts include 81,759 lines already current from D1, D3's 18,926 include 13,281 from D2,
  and D4's 1,961 unchanged are the only cross-delivery matches (same writer; 4,147 of 7,186 ignoring TotPmt/TotAdj).
- Late-file replay of D1 leaves current rows unchanged (81,759 unchanged), but only because its identities
  are reachable by no later delivery; it proves ordering, not idempotence.
- Vanish simulation (`postdate`, filename window): under all-columns identity D2 retires all 81,759 D1 rows,
  D3 retires 13,281 D2 rows, D4 retires 5,225 D3 rows; under the `(Service_ID, VoidSign)` key nothing vanishes
  and the same lines become unchanged or corrected. Both end at 250,643 current rows.

## Presented

- Current rows 350,908 vs 250,643 distinct charge lines across the union (by `(Service_ID, VoidSign)`), which
  the walk's own figures reproduce: 81,759 (all of D1) + 13,281 (D3 in D2's window) + 5,225 (D4 in D3's window
  less the 1,961 unchanged) = 100,265 stale copies, 28.6%. Fee over current rows 276,048,997.56 vs
  190,724,264.08 over the union: overstated by 85,324,733.48. Void pairs net to zero Fee in the union (D2: 11,606
  voided rows, net 0.00). Membership: D2 only 141,900, D1+D2 75,011, D1+D2+D3 6,748, D2+D3 4,992, D2+D3+D4 1,541, D3+D4 5,645, D4 only 14,806.
- Overlap with `finance.monthly_payments` (40 monthly files, 1,287,742 rows, 84 TransCodes, up to 76 rows
  per line): 244,705 distinct Service_IDs in services (D2 + D4 cover the union) vs 249,532 in payments;
  241,773 in both (98.8% of services), 2,932 services-only (Fee 818,105.42 of 191.9M), 7,759 payments-only
  (payments run to 2026-07-21). `|Fee|` agrees for 241,070 of 241,773 matched lines (99.7%); payments `Amount`
  equals `Fee` on only 61,254 rows. Payments repeats the charge line on every transaction; services is the
  charge grain. The reports overlap on content, not on grain.

## Proposed event-date column

`postdate`. It is never blank, parses in every row, is the date the deliveries are cut on (0 rows outside the
filename window in any file, 0 rows after the as-of), and overlapping windows carry identical line sets by it.
Window = filename start..end, with D2's effective start 2023-03-29. `svcdate` is not suitable: it precedes
the window in up to 1,260 rows per file and never bounds a delivery. `pif_4` must be excluded from any
"latest event date" floor because 28 D1 rows are dated 2027. `dob`, `billdate*`, `pif_5` are not events.

## Open questions for the developer

1. Declare `postdate` as the event date for the vanish rule and the as-of floor? (yes/no)
2. Adopt `natural_key: [service_id, voidsign]` (0 duplicates in all four files; voucher is mutable state and
   adds nothing), or keep keyless and rely on the vanish rule, accepting retire-and-insert for every
   re-delivered line and a 28.6% row / $85.3M Fee overstatement until it exists? (named decision)
3. Compute the as-of floor over the declared event date only, not every date column (`pif_4` reaches 2027)? (yes/no)
4. Retype `totpmt`, `totadj`, `totref` as `currency`, `isvoided` as `boolean`, `voidsign` and `encounter` as
   `integer`, aligning with `finance.monthly_payments`? (yes/no)
5. If keyless stays, canonicalise identity text (case of `isvoided`, format of `postmonth`, negative notation
   of `encounter`) so the two writers can match, or accept that they never will? (named decision)
6. Treat D1 as superseded in full by D2 (0 lines only in D1, same Fee to the cent) and load it as history
   only, or load it first as the walk does? (named decision)
7. Set `coverage_start` to 2023-03-29 for the history file, or confirm no charges posted Jan-Mar 2023?
   (named decision)
8. Correct the spec `notes`: 8 `Textbox*` columns not five, and the duplicates are void-reversal pairs
   rather than an unexplained residual? (yes/no)
9. Is Monthly Services still needed for revenue when 98.8% of its lines, with equal Fee in 99.7%, are carried
   on Monthly Payments; or is it kept as the charge-grain source and payments as the transaction grain?
   (named decision for finance)
10. Classify `patcomments1` and `contcomments` (free text, not probed) as `direct` and the `dxcode*` /
    `dxdesc*` columns as `quasi`? Today none carries a pii class. (yes/no)

## Probes run

- Header comparison per file: column count, BOM, line ending, Textbox positions, SSN position, missing columns.
- probe1: per file raw newline and data-line counts, byte-identical and case-folded line groups, blanks and whitespace per column, date format classes and min/max/by-year, rows outside the window per date column, money and numeric shapes, Fee and Units sums, distinct counts and shapes for flags and ids, duplicates for eleven key candidates, PostDate vs SvcDate.
- probe2: normalised cross-writer comparison, VoidSign key candidates, void-pair structure, pairwise overlap asymmetry and per-column drift on shared lines, union counts and membership, vanish simulation under two identities, csv-module row, blank-row and embedded-line-break counts.
- probe3: Service_ID and (Voucher_Number, Service_ID) overlap with the 40 Monthly Payments files, Fee agreement, Amount-equals-Fee count, services rows by year present or absent in payments.
- probe4: (Service_ID, VoidSign) uniqueness in D1/D2, IsVoided case shape, blank-record position in D3/D4, union Fee and overstatement, Fee and PostDate stability per identity, unpaired voided rows per file.
