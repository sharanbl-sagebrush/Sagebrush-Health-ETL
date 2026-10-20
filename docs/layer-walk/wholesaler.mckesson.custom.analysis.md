# Review: wholesaler.mckesson.custom

**Status 2026-10-20:** questions 1, 4 (WAC date and AWP excluded; submitter id kept) and 8
applied; 2 is answered by D9 (both reports load, in their own tables; which one feeds a
figure is a presentation decision); 3, 5, 6, 7 and 9 left open, all recorded in
`docs/decision-record.md`, "McKesson Custom: declared alongside". The walk report was
regenerated the same day: corrections 30 to 11, everything else unchanged.

Reviewed 2026-10-15 against `docs/layer-walk/wholesaler.mckesson.custom.md` (generated the same day)
and `specs/wholesaler.mckesson.custom.yaml`. Four distinct deliveries, all CSV; a fifth file is a
byte-identical copy of the 2023-2026 history file in a second folder and is correctly refused. Deliveries
in as-of order: D1 = Jan012025-Feb042026 (as-of 2026-02-04), D2 = 01012023-03062026 (2026-03-06),
D3 = 01012026-04062026 (2026-04-06), D4 = 03012026-06102026 (2026-06-10). "The invoice report" below
means the sibling `wholesaler.mckesson.invoice` and its finished review.

## Verdict

Safe to load as-is. One variant binds every delivery, zero rows quarantine, landed line counts equal the
physical lines, the dedup rule produces no conflicts, the 30 cross-delivery corrections touch only three
non-financial snapshot columns, within-file duplicates are real split shipments with stable occurrence
numbers, the late replay changes nothing, and the presented total equals a clean month-by-month union.
The finding that matters is scope: from 2026-03 this report is a strict subset of the invoice report. The
June custom extract has 272 rows against the invoice report's 983; the 711 missing rows (extended price
1,629,450.85) belong to 2 accounts that appear in no custom delivery. Through 2026-02 the two reports are
interchangeable row for row (6,413 of 6,415 current custom rows match an invoice-report row on invoice,
item and occurrence with equal money, quantities, dates, accounts and NDCs). CLAUDE.md open question 2
should be answered "invoice report", or the custom report re-pulled with the same account scope first.

## Deliveries and as-of

- All four are CSV, so as-of is the filename end date. No row is dated after its as-of (0 in each) and
  none before the filename start date (0 in each). D1's last event date equals its as-of exactly.
  D3's filename ends 04-06 where the invoice report's D3 ends 04-05; both carry the same 231 rows with
  a max event date of 2026-04-03, so the one-day difference in as-of is cosmetic.
- Overlaps: D1/D2 share 701 identities; D2/D3 181; D3/D4 72; D2/D4 22. Inside every shared window
  0 identities exist only in the older file. Exactly 1 identity (dated 2026-02, invoice number never
  seen in D1) exists only in the newer file of the D1/D2 and D1/D3 pairs: a late-posted invoice.
- Account scope: the invoice review's finding does NOT hold here, it inverts. Custom D4 has 12 accounts,
  0 of them new relative to D3 (13) or D2 (60). The invoice report's D4 has 14; its 2 extra accounts
  carry 711 rows (2026-03 210, 04 230, 05 197, 06 74) and are absent from all four custom deliveries.
  March 2026 is stable across custom extracts (22 rows in D2, 62 in D3 and D4); the invoice report's grew to 272.
- Empty months inside coverage: 2023-01, 2023-02 (D2); 2025-03, 2025-04 (D1, D2). 2025 is 616 rows over
  24 accounts against 3,582 over 57 in 2024. Same picture as the invoice report.
- `wac_effective_date_current_date` is not an event date: it is later than the row's invoice date on
  448 / 4,543 / 11 / 2 rows per delivery, has exactly one value per item within any file (0 items with
  two), and when it moved between D1 and D2 (19 rows, 3 items) it always moved to a later date beyond the
  row's invoice date. It is a current item-master attribute captured at extract time.

## Landed

- Header: 36 columns, identical in name and order across all four deliveries (0 added, 0 removed).
  Position 0 is unnamed and holds a 1..n row number in every file, excluded as the spec says.
  No extra, missing-optional or prohibited columns in any delivery.
- Physical lines after the header, counted from the raw bytes, equal the csv record count and the report's
  landed data lines in every file: 701, 6,165, 231, 272. 0 blank lines; every record has 36 cells.
- No footer, confirmed: 0 records have both key cells blank; the last record carries keys. No BOM; D3 is CRLF, the rest LF.

## Typed

- 0 quarantined cells in any delivery; no spec change is needed for parsing.
- Date drift: D3 ships both date columns as US `m/d/Y` (231 of 231); D1, D2, D4 ship ISO. Both formats
  are declared, ISO first, and D3's rows span four months with 0 failures, so the second format is right.
- Money drift: D3 writes its one negative as a parenthesised amount; D1, D2, D4 use a leading minus
  (1, 31, 6 rows). `awp_current` arrives with 0, 1 or 2 decimals and comma grouping; all parse.
- Numeric-scale drift: D3 ships `received_quantity` and `average_quantity_per_invoice_pkg` as integers,
  the other files as 2- and 1-decimal values. The row hash is scale-insensitive (verified: 0 and 0.00
  hash identically), so this creates no spurious corrections.
- NDC: `ndc_current` is 11 plain digits on every populated row and `n10_digit_ndc_current` is 10 plain
  digits; both are blank together on 63 rows of D2 (0 rows with only one blank). `price_per_package`
  null 21 / 435 / 10 / 9; in D2 372 of those have an NDC, so it is not the non-drug population.
- Always-null columns: `user_pkg_size`, `po_memo` (2 D2 rows excepted), `standard_suggested_retail_price`
  (1 D2 row excepted). `received_quantity` is 0 on all 7,369 rows - a dead column. `order_submitted_by_id`
  null 28 / 1,069 / 13 / 23; `po_number` null on 1 D2 row (the key still forms); WAC date null on 71 D2 rows.
- Control totals (no footer, so our own sums): `extended_price = net_purchases` on every row of every file
  (0 exceptions), so the two control totals are the same number. `gross_purchases` differs from
  `extended_price` only on credit rows (1 / 31 / 1 / 6, exactly the rows with a negative extended price),
  where gross is 0; the differences (808.62, 85,259.27, 2,598.51, 27,911.76) equal the invoice report's
  `gross_credits_and_returns` for D1-D3. `net_quantity = invoice_quantity_filled` on every row.
  `ordered_quantity` is blank on exactly the credit rows (1 / 31 / 1 / 6) where the invoice report carries 0.
  `ordered_quantity = filled + omitted` fails on 3 / 125 / 1 / 12 rows; `= total_ordered_quantity` on 0 / 4 / 0 / 0.
- Credits: 1 / 31 / 1 / 6 rows under 1 / 27 / 1 / 6 credit numbers; a credit number never equals an invoice number.

## Curated

- Corrections between deliveries: 26 (D1 to D2), 3 (D2 to D3), 1 (D3 to D4). Value-level differing
  columns: `wac_effective_date_current_date` 19 + 2, `order_submitted_by_id` 9 + 1 + 1 (1 of the 9 filled
  from null, the rest changed), `awp_current` 2. No financial, quantity, key or date column changes.
  With those three columns excluded, 0 corrections remain between any pair of deliveries. These are
  vendor-side snapshot attributes (item master) and after-the-fact submitter edits, not reorderings (0)
  and not a sign the identity columns are wrong. The cost is that each supersedes a row and moves its
  "last set by" delivery for a change that is not a purchase change.
- Within-file duplicate identities: D2 only, 3 groups holding 7 rows (4 extra rows, matching finding 16),
  max occurrence 3, none byte-identical; 1 group adjacent in the file. Differing columns inside a group:
  `price_per_package`, `ordered_quantity`, `extended_price`, `net_purchases`, `gross_purchases` (2 each),
  `net_quantity`, `invoiced_price_per_dose`, `average_quantity_per_invoice_pkg`,
  `invoice_quantity_filled`, `total_ordered_quantity` (1 each): split shipments. Under the invoice
  report's two-column key the same file has 8 groups / 21 rows, so `po_number` resolves 5 of 8 collisions.
  Occurrence numbering held: 0 identities have a different occurrence count in any two deliveries.
- Overlapping deliveries agree on shared rows apart from the snapshot edits: 675 of 701, 178 of 181,
  21 of 22, 71 of 72 shared identities hash-identical.
- Late-file replay of D1: 27 late, 674 unchanged, 0 current rows changed. Correct.

## Presented

- 6,415 current rows over 6,411 distinct identities (4 extra rows are the occurrence-2+ lines).
- Current `extended_price` 21,675,845.91 equals, to the cent, the sum taken month by month from the
  newest delivery covering each month, so no month is counted twice across the overlaps. 32 months come
  from D2, 2026-01..02 from D3, 2026-03..06 from D4. Current rows by year: 2023 1,786; 2024 3,582;
  2025 616; 2026 431. 37 credit rows are current.
- Against the invoice report's current state (7,124 rows): 6,413 rows match on (invoice, item,
  occurrence); 2 are custom-only (2024-02, extended price 40.36, extra PO-level lines on invoices the
  invoice report knows); 711 are invoice-only (the 2 accounts above, 1,629,450.85). Per delivery: D1
  701 = 701 and D3 231 = 231 with every compared column equal; D2 6,165 vs 6,163 with 3 shared rows
  differing in extended price only because the custom report splits 2 (invoice, item) keys across PO
  lines (key-level price equal, 2 of 2). Ordered quantity differs on 5 shared D2 rows (+27 in the invoice
  report) and the 2 custom-only rows carry 4: the known 23-unit gap is a split difference, not an error.
  Gross purchases per month equal in 34 of 38 months; only 2026-03..06 differ. Loading both reports as
  purchases would double 21,675,805.55 of extended price.

## Proposed event-date column

`invoice_credit_date`. Required and populated on every row, never exceeds the delivery as-of or precedes
the filename start in any delivery, and the vendor's report filter is evidently applied to it. The only
other date, `wac_effective_date_current_date`, is an item-master snapshot that postdates the invoice on
thousands of rows and is null on 71. As in the invoice review, vanish must be scoped by (event date,
`account_number`): a later custom extract without the 2 June accounts would otherwise be read as retiring
rows this report never carried.

## Open questions for the developer

1. Declare `invoice_credit_date` as the event-date column in the spec: yes/no?
2. Answer CLAUDE.md open question 2 as "the invoice report is the purchase source", given the custom
   June extract lacks 2 accounts and 711 rows the invoice report has: yes/no?
3. If the custom report stays in scope, re-pull the 2026-03..06 custom extract and confirm whether the 2
   accounts are absent from the vendor's "R and S" view or were filtered out: named decision on scope of record.
4. Should corrections driven only by `wac_effective_date_current_date`, `awp_current` and
   `order_submitted_by_id` (30 of 30 here) supersede the row, or should those columns be excluded from the
   row hash (a spec field that does not exist yet): yes/no on excluding them?
5. `received_quantity` is 0 on all 7,369 rows across four deliveries: keep it in the spec: yes/no?
6. `net_purchases` equals `extended_price` on every row: keep both as control totals: yes/no?
7. Blank `ordered_quantity` on credit rows types as null here and 0 in the invoice report: should the
   reconciliation treat null as 0 so the two reports compare directly: yes/no?
8. Scope the vanish rule by (date range, `account_number`) rather than date range alone: yes/no?
9. Is the 2025 contraction (24 accounts, 616 rows; nothing in 2025-03 and 2025-04) expected: yes/no?

## Probes run

- Parsed each CSV from raw bytes: BOM, line ending, physical lines after header, csv record count, blank lines, record widths, header order drift, row-number sequence, records with blank key, PO or account cells.
- Classified raw cells by shape per file: both date columns, five money columns (sign notation), both NDC columns (length, hyphens, digits), ten numeric columns (integer / decimal places / blank), AWP decimal places.
- Loaded each file with the real loader; min/max of both date columns, rows after as-of and before coverage start, null counts per column, Python types, negatives per control column, credit rows, credit-vs-invoice number overlap, six per-row arithmetic identities, collisions under the three- and two-column keys, distinct accounts / invoices / items / NDCs / OPAIDs / stores / POs, rows per month, accounts per year.
- Grouped typed rows by identity hash per file; counted groups, differing columns inside a group and adjacency.
- Keyed every file by (identity hash, occurrence); per pair counted shared identities, hash-identical ones, differing columns at string and Decimal level, and in-window identities present on one side only with month, account novelty and invoice novelty; checked occurrence-count stability.
- Checked WAC date per item within a file, direction of WAC and submitter changes between deliveries, corrections remaining with the three snapshot columns excluded, and that `stable_hash` is Decimal-scale insensitive.
- Replicated the dedup rule in memory including the late replay; compared current `extended_price` with a month-partitioned union from the newest delivery per month; current rows per year and per source delivery; March-2026 per extract.
- Loaded the four invoice-report deliveries with the real loader; joined each custom delivery and then the two final current states to the sibling on (invoice number, item number, occurrence); counted shared / one-sided rows, column equality, account-set differences, per-month row counts and gross purchases, key-level price equality for differing rows, and the ordered-quantity gap decomposition.
