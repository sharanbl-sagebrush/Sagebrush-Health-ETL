# Review: wholesaler.cardinal.invoice

Reviewed 2026-10-15 against `docs/layer-walk/wholesaler.cardinal.invoice.md` (generated the same day) and
`specs/wholesaler.cardinal.invoice.yaml`. Seven files match the pattern: four detail deliveries, the 9-column
summary workbook (present twice, byte-identical, the copy correctly refused) and a 14-row roll-up CSV that is
not a vendor file. Detail deliveries in as-of order: D1 = `Invoice Review Jan012025-Feb042026.csv` (as-of
2026-02-04), D2 = `Invoice 01012026-04052026.xlsx` (2026-04-05), D3 = `Invoice Review 01012023-04162026.csv`
(2026-04-16), D4 = `Invoice 03012026-06102026.xlsx` (2026-06-10). S = `Invoice 01012023-03062026.xlsx` (stamp 2026-03-08).

## Verdict

Safe to load the four detail deliveries as-is: every one binds to `invoice_detail`, 0 rows quarantine, the
vendor footer agrees on all three control columns in all four files, every overlapping row is hash-identical
across deliveries (0 corrections, 0 conflicts, 0 within-file duplicate identities), the rule removes exactly
the 1,914 rows / $1,387,175.46 that CLAUDE.md measured, and the late-file replay changes nothing. One decision
rests on a wrong premise and should be reopened: the detail report does not "cover the same period" as the
summary. Detail history starts 2024-04-21; the summary runs from 2023-03-07 and carries 5,326 invoices worth
$16,164,933.79 (2,983 in 2023, 2,343 in 2024-01..04) that exist in no detail file. Where the two overlap (7,997
invoices) they agree to the cent on every invoice. `invoice_date` should be declared the event-date column,
with the caveat that an extract never contains rows dated on its own as-of day.

## Deliveries and as-of

- xlsx stamps vs filename end date: D2 2026-04-05 = filename; D4 2026-06-10 = filename; S 2026-03-08 vs
  filename 03-06 (2 days later, a late download; its data ends 03-06). Stamps are UTC; S's local save was the
  evening of 03-07.
- Provenance: D4's `app.xml` says Apache POI (the portal); D2 and S say Microsoft Excel, with `modified`
  53 seconds and 3 hours after `created`. Two of three workbooks were re-saved by a person; the created stamp
  survived and matches the filename here, but the as-of rule is trusting a stamp Excel is free to rewrite.
- Every file's data stays inside its as-of and filename start: 0 rows after as-of, 0 before coverage start, in
  each of D1..D4. Max event date equals the as-of in D1 and D4 and is one day before it in D2 and D3. Min date
  in D3 is 2024-04-21, not 2023-01-01: the filename overstates.
- Lookback bounds each pull, not the filename: S starts exactly 36 months before its stamp (2023-03-07); D3
  starts within 5 days of 24 months before its as-of (no invoice exists 2024-04-12..04-20 in either report).
  Hypothesis: the detail report is capped at 24 months and cannot recover 2023-03..2024-04 at line level.
- Same-day gap: D3 (as-of 04-16) has 0 rows dated 04-16; D4 has 84 rows dated 04-16, on 24 invoices unseen in
  D3, all on accounts D3 already has. Every day 04-01..04-15 matches D3 row for row. Overlaps before the as-of
  day are complete: D1 vs D3 5,482 = 5,482; D2 vs D3 1,272 = 1,272; D2 vs D4 538 = 538.
- The McKesson account-scope pattern does not occur: 0 rows in any shared window belong to an account absent
  from the other file. The 1 account in D4 absent from D3 first appears 2026-06-09.
- `Cardinal Invoices.csv` (DB_Validation_Pack/Reconciliation_Prep): 8 columns (`Wholesaler`, `Account #`,
  `Account Name`, `Account Alias`, `DistinctInvoices`, `LineItems`, `FirstInvoiceDate`, `LastInvoiceDate`),
  14 rows over 12 accounts (2 split by `Account Name`). The spec does not claim it (UNRECOGNISED, correct). It
  is a roll-up derived from S, not the detail: `DistinctInvoices` equals S's row count on 12 of 12 accounts,
  `LineItems` equals `DistinctInvoices` on every row (1,353 = 1,353), and it matches the detail on 2 of 14.
  The pattern `cardinal.*invoice` will keep finding it.

## Landed

- Header: 17 columns, identical in name and order across D1..D4 (0 added, 0 removed). The summary has 9 of
  them; the detail-only 8 are `CIN`, `Generic Name`, `Invoice Line #`, `NDC`, `Original Ordered Qty`, `Ship Qty`,
  `Trade Name`, `Unit price (Current Invoice)`. No extra, missing-optional or prohibited columns anywhere.
- Physical lines after the header equal the report's "data lines" in every file: D1 5,483, D3 20,603 (CSV,
  0 blank lines, every record 17 cells); D2 1,273, D4 1,510 (xlsx, 0 fully blank rows). Each is typed rows
  plus the one footer line, always last. `skip_trailing_blank_rows` has nothing to drop on any file.
- Encoding: no BOM anywhere; D1 is CRLF, D3 LF.

## Typed

- 0 quarantined cells; no spec change is needed for parsing.
- Date drift handled: D1 ships `Invoice Date` as US `m/d/Y` (5,482 of 5,482), D3 as ISO (20,602 of 20,602),
  D2 and D4 as native xlsx datetimes with zero time. The US parse is proven, not assumed: all 5,482 D1 rows are
  hash-identical to their D3 (ISO) rows, and the hash covers the date.
- Money drift handled: D1 writes `$x ` with a trailing space (5,200 cells) and parenthesised negatives (282);
  D3 writes `$x` and `-$x` (1,300); xlsx cells are numeric. The spec's "trailing space" note is true of D1 only.
  Negative `purchase_amount` rows equal `Return` rows exactly in every file (282, 44, 1,300, 19), as do negative
  `shipped_quantity` rows. `unit_price * shipped_quantity = purchase_amount` holds within a cent on every row.
- `ordered_quantity` is 0 on all 1,300 returns and on 890 purchases in D3 (exactly the rows where shipped
  exceeds ordered). The footer still sums it, so the control holds, but it is not a quantity of record.
- NDC: every data row carries 11 plain digits (0 blank, 0 hyphenated, 0 ten-digit). The spec comment "20,602 of
  20,603" counts the footer as the row without NDC. `account_number` and `invoice_number` are 10 characters on
  every row; `invoice_line_number` is 6 characters with a leading zero on every row, so text is right.
- Footer: 3 cells at positions 14-16 in every file. The $20.1M figure is verified, not refuted: an independent
  csv parse of D3 sums `purchase_amount` to 20,147,607.39, `ordered_quantity` to 38,779 and `shipped_quantity`
  to 37,734, equal to the footer and the loader; it is 22,203,107.27 of purchases net of -2,055,499.88 of
  returns. What it is not is three years of history (see Deliveries).
- `Credit Rebill Invoice` and `Credit Rebill Credit` come in equal counts in every file (844/844 in D3); all
  197 credit groups by (account, date) have a rebill invoice on the same account and date with the same NDC
  set. The summary carries 2 invoice types the detail never has (72 and 1 rows, all before 2024-04).

## Curated

- Within-file duplicate identities: 0 in every delivery. `(invoice_number, invoice_line_number)` alone is also
  unique in every file and no invoice spans two accounts, so `account_number` in the key is a safeguard.
- Shared identities are hash-identical in every pair: D1/D2 421, D1/D3 5,482, D2/D3 1,272, D2/D4 538, D3/D4
  642. Corrected rows: 0, so no differing columns to name; no reorderings; no sign the identity is wrong.
- Naive union of D2+D3+D4: 23,383 rows, 21,469 identities, 1,914 duplicate rows (8.2%) summing to
  1,387,175.46. The rule removes exactly that overlap. With D1 the naive union is 28,865 rows and 7,396
  duplicates (10,971,231.27), reduced to the same 21,469.
- Replay in as-of order reproduces the report: 5,482 inserted; 851 / 421 unchanged; 14,269 / 6,333; 867 / 642.
  Late replay of D1: 5,482 unchanged, 0 late, 0 current rows changed.
- Vanish is not simulated, and the same-day gap matters for it: a delivery with as-of X covers dates up to
  X-1, not X, or the 84 rows dated 04-16 would be judged against a pull that could not contain them.

## Presented

- 21,469 current rows: `purchase_amount` 20,506,622.51, `ordered_quantity` 40,114, `shipped_quantity` 39,067.
  The amount equals, to the cent, a month-partitioned union taking each month from the newest delivery
  claiming it (2024-04..2026-02 from D3, 2026-03..06 from D4); no month is counted twice.
- Current rows by year: 2024 14,165; 2025 5,061; 2026 2,243 (by month 348, 386, 467, 427, 508, 107).
  2026-04 is 79,975.53 in D2, 144,521.88 in D3, 249,931.28 in D4; 2026-01..03 are identical wherever present.
- 142 current accounts: 133 first seen in 2024, 8 in 2025, 1 in 2026; last seen 2024 for 37, 2025 for 53,
  2026 for 52.
- What presented does not hold: 2023-03-07..2024-04-20, which the summary has as 5,326 invoices and
  $16,164,933.79 across 197 accounts (56 of them never in any detail file).

## Proposed event-date column

`invoice_date`. It is the only date column, required and populated on every row, parsed from three physical
shapes to the same value, never later than the as-of and never earlier than the filename start in any
delivery, and an invoice has exactly one date (0 of 8,212 invoices span two). Caveat for the vanish rule:
coverage of a delivery is [start, as-of - 1 day], because the portal excludes the extraction day (0 rows
dated 04-16 in the 04-16 pull, 84 such rows in the next one).

## Open questions for the developer

1. Reopen `invoice_summary: recognise_only` and load the summary for dates before the earliest detail row
   (2024-04-21), so 2023-03..2024-04 ($16.2M, 5,326 invoices) reaches the warehouse: yes/no?
2. If yes, identity for summary rows is `(account_number, invoice_number)` with `invoice_line_number` null: yes/no?
3. Confirm with Cardinal that the Invoice Review detail report is capped at 24 months: yes/no?
4. Declare `invoice_date` as the event-date column in the spec: yes/no?
5. Define delivery coverage for vanish as ending the day before the as-of: yes/no?
6. Exclude `DB_Validation_Pack` from discovery, or tighten the pattern, so the derived roll-up stops surfacing
   as UNRECOGNISED on every walk: named decision.
7. Accept an xlsx `created` stamp when `app.xml` says Microsoft Excel (a human re-save), or require the
   filename date to agree within N days: named decision (N).
8. Correct the spec comments "covers the same period", "2024 to 2026 in full" and "NDC on 20,602 of 20,603": yes/no?
9. Treat `ordered_quantity` (0 on 890 purchases and all returns) as informational, not a reconciliation quantity: yes/no?
10. Credit-rebill pairs (844 + 844 lines in D3) net to the rebilled amount; collapse them or sum as-is: named decision.

## Probes run

- Parsed each CSV from raw bytes: BOM, line endings, physical lines, blank lines, record widths, key-blank records, footer positions, date/money/quantity/NDC cell shapes, line-number lengths, blanks per column, distinct accounts and invoices.
- Read each xlsx: core.xml created/modified, app.xml application, sheets, Python type per column, time-of-day on dates, footer cell types, negative and non-integer counts.
- Compared the five header rows for name and order drift, detail vs summary.
- Loaded D1..D4 with the real loader: outcome, quarantine, footer agreement, control totals, min/max `invoice_date` vs as-of and filename start, rows per month and year, sign vs `purchase_or_return`, the unit-price identity, ordered-vs-shipped counts.
- Keyed every delivery by (identity hash, occurrence); counted within-file groups, `(invoice, line)` uniqueness, invoices spanning accounts; per pair, shared identities, hash-identical ones, differing columns, and one-sided rows inside the shared claimed window with their accounts, invoices, months and lag.
- Replicated the dedup rule in as-of order, the naive unions (D2+D3+D4, D1..D4), the late replay, the month-partitioned union, current rows by source, year, month and account first/last-seen year.
- Joined the summary to the detail by invoice number: counts and amounts per year, shared vs one-sided invoices, amount/date/account agreement, account sets, invoice-type mix, lookback boundaries by day; compared D1 to the summary within D1's window.
- Profiled `Cardinal Invoices.csv`: columns, rows, accounts, which columns differ on split accounts, and whether its counts and dates match the summary or the detail per account.
- Per-day row counts around each as-of and lookback boundary; credit-rebill pairing by (account, date, NDC set).
