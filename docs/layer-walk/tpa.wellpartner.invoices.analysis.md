# Review: tpa.wellpartner.invoices

Reviewed 2026-10-15 against `docs/layer-walk/tpa.wellpartner.invoices.md` (generated the same day)
and `specs/tpa.wellpartner.invoices.yaml`. Four distinct deliveries; a fifth file is a byte-identical copy
of the 2023-2026 history workbook in a second folder and is correctly refused. Deliveries are referred to
in as-of order: D1 = Jan012025-Feb052026.csv (as-of 2026-02-05, filename), D2 = 01012023-03062026.xlsx
(2026-03-08, stamp), D3 = 01012026-04052026.xlsx (2026-04-06, stamp), D4 = 03012026-06102026.xlsx (2026-06-10, stamp).

## Verdict

Safe to load as-is, with two decisions needed before the presented layer is used for reconciliation and one
before the vanish rule is built. Every delivery binds to the one variant with an identical 10-column header,
zero rows quarantine, there are zero corrections and zero conflicts across the overlaps, the late-file replay
changes nothing, and the presented total equals a clean newest-delivery-per-month union to the cent. But:
(1) the spec's promise that the ten stray-space rows "collapse as true duplicates" does not hold under the
decided rule - they survive as occurrence 1 and 2, so the presented layer carries 10 invoices twice
(43,895.62); (2) the 323 credit rows carry a positive sign, so the control total sums credits as purchases
(137,634.52); (3) D3's filename claims 2026-01-01..04-05 but the file holds nothing before 2026-03-13, so
filename coverage cannot drive vanish for this source.

## Deliveries and as-of

- Stamps vs filename end dates: D2 stamp 2026-03-08 vs filename 03-06 (2 days later; `docProps/core.xml`
  modified 12 minutes after created, with creator and lastModifiedBy both populated, so the workbook was
  touched after export). D3 stamp 2026-04-06T00:39Z vs filename 04-05 (same evening in US time). D4 stamp
  2026-06-10T23:33Z vs filename 06-10. D1 is CSV, filename only. No stamp precedes its file's max event date.
- Data stays inside every as-of: 0 rows after as-of in each delivery; 0 rows before the filename start.
  Max event date vs as-of: D1 0 days, D2 2, D3 3, D4 0. Rows dated within 7 days of as-of: 57, 37, 26, 58.
- Data does NOT fill the filename window in D3: filename 2026-01-01..04-05, data 2026-03-13..04-03, 89 rows in
  ISO weeks 11-14 only. D2 holds 317 rows dated 2026-01-01..03-06 and D4 holds 43 rows (64,187.99) dated
  2026-03-07..03-12 that no other delivery carries. D3 looks like a "since the last extract" pull, not a
  date-filtered one, and the week 03-07..03-12 was only recovered because D4 arrived.
- Overlaps agree: D1 x D2 (2025-01-01..2026-02-05) 1,060 identities shared, all hash-identical; D2 holds 1
  extra row dated 2026-02-05 (1,008.44), posted the same day after D1 was pulled. D2 x D4 (03-01..03-06) 37/37
  identical. D3 x D4 (03-01..04-05) 89/89 identical, D4 adds 80 rows dated 03-04..03-12.
- Only 2 wholesalers appear in every delivery (McKesson-class accounts are 6 digits, Cardinal-class 10) and
  2 covered-entity IDs; the second ID has 379 rows, 2023-05..2025-07, and is absent from D3 and D4.
- Empty months: 2025-03 and 2025-04 (same gap as the McKesson wholesaler extract). 2025 holds 888 current
  rows against 4,197 in 2024.

## Landed

- Header: 10 columns, identical in name and order across all four deliveries (0 added, 0 removed). No extra,
  missing-optional or prohibited columns. CSV and xlsx both bind to `invoices_10` on the same 10 targets.
- Physical rows equal the report's landed lines: D1 1,061 physical lines (CRLF, no BOM, every record 10
  cells) = 1,060 data rows; D2/D3/D4 single sheet, `max_row` = data rows + 1, 0 blank rows: 7,700, 89, 523.
- Raw cell types in the workbooks: every column is text except `Invoice Net Amount` (float 7,569/87/514,
  int 131/2/9). The date column is a text cell in m/d/Y on 7,700 + 89 + 523 of 8,312 cells; D1 is m/d/Y text
  on 1,060 of 1,060. `Hub-Spoke` is non-blank on 3 rows in D1, 1,246 in D2, 0 in D3 and D4.

## Typed

- 0 quarantined cells in any delivery. No spec change is needed to parse these files.
- The native-datetime bypass does not bite here: no workbook cell in the date column is a datetime, all are
  m/d/Y strings and go through `date_formats`. It would bite silently if the vendor ever exported real
  date cells (parse_date takes `.date()` without consulting the declared formats) - worth a test, not a spec change.
- Money: xlsx floats parse through `Decimal(repr())` to 0-2 decimal places (no binary artefacts: typed
  amounts have 0/1/2 places only); CSV amounts are bare numbers, no `$`, no parentheses. 0 negative values in
  any delivery. Sign finding: all 323 credit rows (D1 4, D2 323 by invoice type; 2023 32, 2024 286, 2025 3,
  2026 2) carry a POSITIVE amount, so `invoice_net_amount` as a control total sums credits as purchases.
  D3 and D4 carry only one `invoice_type` value (0 credits). A credit's invoice number never equals a
  non-credit invoice number in the same file (0 of 323). Zero-amount rows: 3 in D1, 56 in D2, all non-credit.
- Shapes: `wholesaler_invoice` 10 digits on every row; `wholesaler_account` 6 or 10 digits; `purchase_order`
  never equals the invoice number (0 rows); 0 rows have leading or trailing whitespace in any key column.
- Spec-change candidate, named not made: `entity_name` - the 10 stray rows differ only by a space inside the
  parenthesis; a typing-time whitespace normalisation (or the pattern-extracted 340B id the spec comment
  proposes) makes the pairs byte-identical. It would NOT collapse them under the decided rule (see Curated).

## Curated

- Key uniqueness: unique in D1 (1,060), D3 (89), D4 (523). D2 has 7,690 distinct keys over 7,700 rows: 10
  pairs, all adjacent in the file, differing only in `entity_name` (2023-05: 1, 2023-06: 9), all McKesson-class.
  Adding `entity_name` to the key gives 7,700 distinct, i.e. the pairs are exactly the stray-space rows.
- The rule keeps both: identity = key + occurrence, so the pairs land as (key,1) and (key,2) and both are
  current. The spec comment's "collapse as true duplicates" is not what happens. All 10 invoice numbers
  exist once on the McKesson wholesaler side, and 9 of 10 at exactly one copy's amount - they are the same
  invoice twice, and the presented sum overstates by 43,895.62. Byte-identical rows would also stay two
  occurrences (that is the rule's Allscripts behaviour), so normalising the space does not fix this alone.
- Corrections between deliveries: 0 (D1 to D2), 0 (D2 to D3), 0 (D3 to D4); no columns differ on any shared
  identity. Occurrence counts are stable across deliveries. Late-file replay of D1: 0 late, 1,060 unchanged,
  0 current rows changed. Correct.

## Presented

- 8,186 current rows = 8,176 distinct keys + 10 second occurrences; 9,372 landed rows, 8,186 distinct row
  hashes. Current `invoice_net_amount` 22,227,253.00 equals the sum taken from the newest delivery per month
  (2025-12..2026-02 from D2, 2026-03..06 from D4). Net of credits it is 21,951,983.96. By year: 2023 2,298;
  2024 4,197; 2025 888; 2026 803. By class: McKesson 2,664, Cardinal 5,522, ABC 0.
- Against the wholesaler sources (current rows, invoice level, credits netted on the TPA side):
  - Dates agree exactly: on all 2,506 matched McKesson and 3,422 matched Cardinal invoices the TPA's date
    equals the wholesaler's invoice date (lag 0 days, 0 exceptions). Lag is not where the two sides diverge.
  - McKesson 2025: TPA 315 invoices 2,245,977.37; wholesaler 350 invoices 2,451,688.60; 312 matched, 280 to
    the cent, TPA higher on 32 (5,820.43 in total, 21 of them by 100 or more); wholesaler-only 38 invoices
    211,531.66 (72 lines in 2025-01). 2026-01..06-10: 261 of 264 matched, 251 exact; wholesaler-only 336
    invoices 2,092,125.23, almost all 2026-03..06 (the two accounts new in the McKesson June file).
  - Cardinal 2025: TPA 573 invoices 535,474.45; wholesaler 2,082 invoices 9,229,561.64; 572 matched, only 14
    exact but 543 within 1.00, and the TPA is higher on 558 of 572 (359.97 in total) - a systematic
    per-invoice difference, not noise. Wholesaler-only 1,510 invoices 8,694,460.19; accounts TPA 57,
    wholesaler 103, 56 common. The TPA sees about 6% of Cardinal 2025 dollars.
  - ABC: 0 TPA rows against 2,921 wholesaler invoices 18,253,091.91. Wellpartner never reports ABC.
  - Before the wholesaler extracts begin, the TPA is the only record: Cardinal 2,100 invoices 2,094,105.92
    dated 2023-01..2024-04 (the Cardinal extract starts 2024-04-21); McKesson 68 of 148 TPA-only invoices are
    2023-01/02 (the McKesson extract starts 2023-03).
  - Whole range: McKesson matched 2,506 of 2,654 TPA invoices (17,222,102.90 vs 17,156,984.58 on the matched
    set); Cardinal matched 3,422 of 5,522 (2,422,206.94 vs 2,418,194.20).

## Proposed event-date column

`wholesaler_invoice_date`. It is the only date column, required and parsed on every row, never later than
the delivery as-of, and on 5,928 of 5,928 invoices matched to a wholesaler extract it equals the
wholesaler's own invoice date, so it is the wholesaler's event date, not a TPA posting date. Caveat for the
vanish rule: delivery coverage must be taken from the data (min..max of this column) or disabled, never
from the filename - D3 claims 2026-01-01 but holds nothing before 03-13, and vanish on the filename window
would retire 317 current D2 rows. Vendor filter is evidently not this column.

## Open questions for the developer

1. Declare `wholesaler_invoice_date` as the event-date column in the spec: yes/no?
2. Coverage for vanish on this source: filename window, observed data range, or vanish disabled? Named decision.
3. The 10 stray-space pairs: accept two current occurrences (43,895.62 double count) or add a per-source
   exact-duplicate collapse the one rule does not have today? Named decision.
4. Normalise whitespace inside `entity_name` at typing, or add a pattern-extracted 340B id column, as the spec
   comment suggests: yes/no? (Does not change the occurrence outcome on its own.)
5. Should presented purchase totals subtract the 323 positive-signed credit rows by `invoice_type`: yes/no?
6. Cardinal: the TPA net exceeds the summed line `purchase_amount` by under 1.00 on 529 of 572 matched 2025
   invoices and by 1.00-100.00 on 29. Which figure is the purchase of record for reconciliation? Named decision.
7. Is it expected that Wellpartner never carries ABC invoices (0 rows against 2,921 ABC invoices): yes/no?
8. Was D3 pulled as "since the last extract" rather than by invoice date (0 rows before 2026-03-13, and the
   week 03-07..03-12 exists only in D4)? Yes/no - it decides what the operator must request each week.
9. D2's workbook was modified 12 minutes after creation by a named user before upload. Is the xlsx stamp still
   to be trusted as extraction time when created and modified differ: yes/no?
10. Second covered-entity ID (379 rows, last seen 2025-07) and the empty months 2025-03/04: expected business
    change, or missing data? Yes/no on expected.

## Probes run

- Read each workbook's `docProps/core.xml` for created/modified stamps and whether creator fields are populated; compared with filename start/end dates and with min/max of the typed event date; counted rows after as-of, outside the filename window, and within 3/7 days of as-of.
- Counted CSV physical lines, BOM, line endings, record widths; workbook sheets, `max_row`, blank rows; raw openpyxl cell types per column; date cells carrying a time part; float decimal places in the amount column; classified raw text cells by shape.
- Compared the four header rows for name and order drift; listed bound targets, new and missing-optional columns per delivery.
- Loaded each delivery with the real loader; counted quarantine by (column, reason), typed date types, amount decimal places, sign by invoice type, zero-amount rows, distinct values per text column, key-column shapes and lengths, whitespace in key columns, entity-name parenthesis shapes and distinct 340B ids per year.
- Grouped typed rows by natural key per delivery; counted duplicate groups, adjacency, differing columns, their months and amount; recounted distinct keys with `entity_name` added.
- For every pair of deliveries, counted shared identities in the overlapping window, hash-identical ones, differing columns, one-sided rows by month with their lag from the older as-of.
- Replicated the dedup rule in memory; compared current rows and sum with distinct keys, distinct hashes, and a newest-delivery-per-month union; counted current rows by year, month, class and covered-entity id; listed empty months.
- Built current rows for wholesaler.mckesson.invoice, wholesaler.cardinal.invoice (detail variant) and wholesaler.abc.purchases the same way; keyed both sides by normalised invoice number per wholesaler class and window; counted matched / one-sided invoices, exact and within-1.00 amount agreement, difference buckets and direction, date lag on matched invoices, one-sided rows by month, and account overlap.
- Looked up the 10 stray-space invoices on the wholesaler side and compared the wholesaler amount with one and two copies.
