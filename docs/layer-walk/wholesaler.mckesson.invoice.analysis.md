# Review: wholesaler.mckesson.invoice

**Status 2026-10-20:** questions 1, 2 and 7 applied (spec declares the event date,
vanish is scoped by account, the walk column is renamed); 5 and 6 taken as defaults and
3 and 4 left open, all recorded in `docs/decision-record.md`, "McKesson Purchasing
Detail: reviewed and declared". The walk report was regenerated the same day with the
`vanished` outcome running; every count below still holds.

Reviewed 2026-10-15 against `docs/layer-walk/wholesaler.mckesson.invoice.md` (generated the same day)
and `specs/wholesaler.mckesson.invoice.yaml`. Four distinct deliveries, all CSV; a fifth file is a
byte-identical copy of the 2023-2026 history file in a second folder and is correctly refused.
Deliveries are referred to below in as-of order: D1 = Jan012025-Feb042026 (as-of 2026-02-04),
D2 = 01012023-03062026 (2026-03-06), D3 = 01012026-04052026 (2026-04-05), D4 = 03012026-06102026 (2026-06-10).

## Verdict

Safe to load as-is. Every delivery binds to the one variant, zero rows quarantine, the vendor footer
agrees with our sums on all ten control columns in all four files, the dedup rule produces no
conflicts, the eleven cross-delivery corrections are all in one non-financial column, within-file
duplicate identities are stable across deliveries, the late-file replay changes nothing, and the
presented totals equal a clean month-by-month union with no double counting. Two things need a
decision before the vanish rule is built, not before loading: the spec must declare
`invoice_credit_date` as the event date, and delivery coverage must be understood as date range
times account set, because D4 carries two accounts (711 of its 983 rows) that are absent from D2
and D3 even for dates those files claim to cover.

## Deliveries and as-of

- All four are CSV, so as-of comes from the filename end date in every case; the `Jan012025-Feb042026`
  shape parses correctly. No file has an event date later than its as-of (0 rows in each), and no row
  falls before the filename start date (0 rows in each). The as-of dates are right.
- Overlaps: D1 and D2 share 2025-01-01..2026-02-04 (701 identities); D2 and D3 share 2026-01-01..03-06
  (181); D3 and D4 share 2026-03-01..04-05 (72 in D3, 315 in D4); D2 and D4 share 2026-03-01..03-06
  (22 in D2, 73 in D4).
- Account scope drifts between extracts. D4's rows dated inside the D3 window that D3 lacks (243) and
  inside the D2 window that D2 lacks (51) all belong to 2 accounts that appear nowhere in D2 or D3.
  Those 2 accounts are first seen in 2026-03 and carry 711 rows in D4 (210 March, 230 April, 197 May,
  74 June). So the April-5 extract was not complete for March even though its filename says it was.
  The reverse never happens: 0 identities inside any shared window exist only in the older file, so
  nothing would have vanished.
- Posting latency is otherwise small: one identity dated within 7 days of D1's as-of appears only in
  D2 and D3; it has an invoice number D1 has never seen (a late-posted invoice, not a correction).
- Empty months inside coverage: 2023-01 and 2023-02 in D2; 2025-03 and 2025-04 in D1 and D2.
  2025 volume is a fraction of 2024 (616 vs 3,580 current rows; 24 vs 57 distinct accounts).
  Accounts fall out steadily from 2024-04 onward. A business question, not an ETL fault.

## Landed

- Header: 45 columns, identical in name and order across all four deliveries (0 added, 0 removed).
  Position 0 is the unnamed row-number column, excluded as the spec says. No extra, missing-optional
  or prohibited columns in any delivery.
- Physical lines after the header, counted from the raw bytes, equal the report's landed "data lines"
  in every file: 702, 232, 984, 6,164 (each is typed rows plus the one footer line; 0 blank lines,
  every record has 45 cells). The report's column label "data lines" includes the footer line.
- Encoding drift, all handled: no BOM anywhere; D3 is CRLF, the other three LF.
- Exactly one record per file has both key cells blank, and it is the last record: the footer.

## Typed

- 0 quarantined cells in any delivery; no spec change is needed for parsing.
- Date format drift: D3 ships `Invoice/Credit Date` as US `m/d/Y` (231 of 231 rows); D1, D2, D4 ship ISO.
  Both formats are declared, ISO first. D3 cannot be `d/m/Y` because its 231 rows span three months
  with 0 parse failures and a max event date of 2026-04-03, so the second format is the right one.
- Money sign drift: D3 writes negatives as parenthesised amounts (1 row in each of three columns);
  D1, D2, D4 use a leading minus (1, 31, 28 rows). Both parse negative; the per-file negative counts
  equal the credit-row counts in every file.
- Footer: 11 populated cells at the last line in every file; the eleventh is `Invoiced Price Per Dose`,
  which the vendor sums but the spec deliberately does not control-total. The ten declared controls
  agree in all four files. `net_handling_charge_amount` is 0.00 in every file (all rows carry a zero).
- Internal arithmetic: `extended_price = gross_purchases + gross_credits_and_returns` holds on every row
  of every file (0 exceptions). `net_quantity = invoice_quantity_filled + returned_credited_quantity`
  fails on exactly the credit rows (1, 1, 28, 31), so net quantity is defined differently on a credit.
- NDC/UPC: every non-blank value is 11 plain digits (no hyphens, no 10-digit forms). Blank on 63 rows
  of D2 and 1 row of D4. `price_per_package` null (21, 10, 12, 435 rows) is not the same population
  as NDC blank: in D2, 372 rows have an NDC but no package price.

## Curated

- Corrections between deliveries: 9 (D1 to D2), 1 (D2 to D3), 1 (D3 to D4). In all 11 the only
  differing column is `order_submitted_by_id`; no financial, quantity, key or date column changes.
  Real vendor edits (the submitter id is filled in or changed after the fact), not reorderings and
  not a sign the identity columns are wrong. All 9 D1-to-D2 corrections are dated 2026-02, within a
  week of D1's as-of.
- Within-file duplicate identities: 0, 3, 5, 7 groups (8, 12, 18 rows), max occurrence 4, none
  byte-identical. Within a group the differing columns are `original_invoice_number` (3, 5, 5 groups)
  and the quantity/price columns (`extended_price`, `net_quantity`, `ordered_quantity`,
  `invoice_quantity_filled`, `gross_purchases`: 3, 3, 6 groups; `price_per_package` 1, 1, 3;
  `invoiced_price_per_dose` and `invoiced_dose_quantity_extended` 1 in D2). These match finding 16:
  same item on the same invoice referencing different original invoices, plus split shipments.
  Most groups are not adjacent in the file (3, 4, 4 non-adjacent), so occurrence numbering depends on
  the vendor's sort order; it held: 0 identities have a different occurrence count in any two
  deliveries, and 0 corrections were reorderings.
- Overlapping deliveries agree on shared rows apart from the 11 submitter-id edits: 692 of 701,
  180 of 181, 21 of 22, 71 of 72 shared identities hash-identical.
- Credits: 1, 31, 1, 28 credit rows; a credit number never equals an invoice number in the same file,
  and 1, 29, 1, 27 of them carry an original invoice reference. Returns insert beside the original.
- Late-file replay of D1: 9 late, 692 unchanged, 0 current rows changed. Correct.

## Presented

- 7,124 current rows over 7,111 distinct identities (13 extra rows are the occurrence-2+ lines).
- Current `extended_price` 23,305,296.76 equals, to the cent, the sum obtained by taking each month
  from the newest delivery that covers it, so no month is counted twice across the overlaps.
  Months 2023-03..2025-12 come from D2, 2026-01..02 from D3, 2026-03..06 from D4.
- Current rows by year: 2023 1,786; 2024 3,580; 2025 616; 2026 1,142.
- March 2026 is worth knowing about when anyone compares against an older pack: D2 saw 22 rows
  (through 03-06), D3 saw 62, D4 sees 272; `extended_price` for the month moves from 513,610.25
  to 677,375.14 to 1,181,586.17 across those three extracts, almost all of it the two new accounts.

## Proposed event-date column

`invoice_credit_date`. It is the only date column in the file, it is required and populated on every
row, it never exceeds the delivery as-of or precedes the filename start date in any of the four
deliveries, and the vendor's own report filter is evidently applied to it. For a credit it is the
credit date, not the original invoice date, which is right for supersession (the credit is its own
line under its own number). Caveat for the vanish rule: a newer delivery covering a date does not
imply it covers every account on that date (see Deliveries). Vanish should be evaluated within
(event date, account_number) and only for accounts the newer delivery contains, or the two accounts
new in D4 would be retired by any later extract run without them.

## Open questions for the developer

1. Declare `invoice_credit_date` as the event-date column in the spec (field to be added to the model): yes/no?
2. Should the vanish rule scope coverage by (date range, account_number) rather than date range alone,
   given the 2 accounts present in D4 but absent from D2 and D3 for the same dates: yes/no?
3. Were the 2 accounts first seen in 2026-03 newly linked to the portal after 2026-04-05 (so D3 is a
   complete extract of what was visible then), or was D3 run with a narrower account filter? Named
   decision: which is the account scope of record for a delivery.
4. Is the 2025 contraction (24 accounts and 616 rows versus 57 and 3,580 in 2024; nothing in 2025-03
   and 2025-04) expected business change, or a missing extract? Yes/no on expected.
5. Should the footer's `Invoiced Price Per Dose` total be checked too (it is a sum the vendor
   provides), or stay excluded as a rate: yes/no on checking it?
6. Is `net_quantity` on credit rows (where it does not equal filled plus returned, 31 of 31 credit rows in D2)
   the right quantity for reconciliation, or should credits use `returned_credited_quantity`? Named decision.
7. The report's landed column "data lines" counts the footer line; rename to "lines after header"
   or exclude the footer: yes/no?

## Probes run

- Parsed each CSV from raw bytes: BOM, line ending, physical lines after header, csv record count, blank lines, record widths, footer cell count and populated footer column names, records with blank key cells.
- Classified raw `Invoice/Credit Date` cells by shape (ISO / US / blank / other) per file.
- Classified raw cells of five money columns by sign notation (dollar / minus / parenthesised / blank / bare) per file.
- Classified raw `NDC/UPC (Current)` cells by length, hyphenation and digit-only, per file.
- Compared the four header rows for name and order drift.
- Loaded each file with the real loader; took min/max of `invoice_credit_date`, rows after as-of, rows before coverage start, Python types present, negative counts per control column, credit-row and service-line counts, and two per-row arithmetic identities.
- Grouped typed rows by identity hash per file; counted groups, which columns differ inside a group, and whether group members are adjacent in the file.
- Keyed every file by (identity hash, occurrence); for each pair of deliveries counted shared identities, hash-identical ones, the differing columns, and identities inside the shared date window present on only one side, with their month and lag from the older as-of.
- Replicated the dedup rule in memory; summed `extended_price` over current rows and compared with a month-partitioned union taken from the newest delivery per month; counted current rows per month and per year.
- Checked occurrence-count stability of every identity across deliveries.
- Counted distinct accounts, facilities, fill DCs and classifications per delivery and per year; for the rows present only in a newer delivery, counted their distinct accounts and invoices and whether those appear anywhere in the older delivery.
- Diffed the account sets between every pair of deliveries and bucketed the affected rows by month; counted accounts by first-seen and last-seen month.
