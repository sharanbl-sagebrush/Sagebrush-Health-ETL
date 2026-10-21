# Review: dispense.weinfuse.buybill

Reviewed 2026-10-15 against `docs/layer-walk/dispense.weinfuse.buybill.md` (generated the same day) and
`specs/dispense.weinfuse.buybill.yaml`. Three distinct deliveries, all CSV; a fourth file is a byte-identical
copy of the 2023-2026 history file in the second 03062026 folder and is correctly refused. In as-of order:
D1 = Jan012025-Feb052026 (as-of 2026-02-05), D2 = 01012023-03062026 (2026-03-06), D3 = 01012026-04052026 (2026-04-05).

## Verdict

Safe to load as-is, with one cosmetic defect that will mislead anyone reading the ledger: 17,086 of the
17,088 D1-to-D2 "corrections" are not corrections. D1 ships `Date` as US `m/d/Y` and `Created At` as
`m/d/Y H:MM`; D2 and D3 ship ISO. Both land as `text`, so every shared row hashes differently although only
2 rows changed in any other column. Retyping `date` (and deciding what to do with `vendor_created_at`) removes the
noise; nothing is lost or double counted today because D2 supersedes D1 either way. Everything else is
clean: one variant, zero quarantine, header identical across deliveries, key unique in every file, no
within-file duplicates, no conflicts, late replay changes nothing, presented total reconciles exactly.
Three things to know before the vanish rule is built: the vendor's date filter includes the whole day
before the filename start and only part of the as-of day; one D1 row is a genuine vanished line that will
stay current until the rule exists; and this feed is a strict subset of `dispense.weinfuse.administered`.

## Deliveries and as-of

- All three are CSV, so as-of comes from the filename end date; the `Jan012025-Feb052026` shape parses.
  0 rows in any file are dated after its as-of, so the as-of is never too early. The as-of dates are right.
- Drops: 2026-02-05 (repo root), 2026-03-06 (two folders, second copy refused as byte-identical),
  2026-04-05. Drops without a BuyBill file: `March012026-June102026` (only Administered and Patients, both
  xlsx) and `April 16th - Cardinal` (1 file, Cardinal only). The spec's `discontinued` note matches.
- Start boundary: D1 has 19 rows and D3 80 rows dated before the filename start, every one on the day before
  the start, and D2 holds exactly 19 and 80 rows on those two days. The Administered extract of the same
  drop behaves the same way (31 of 31). So the vendor's filter includes the full previous day; the filename
  start is off by one. D2's start (2023-01-01) shows no such rows because no WeInfuse data predates 2023.
- End boundary: the as-of day is partial. D1 carries 50 rows on its as-of day where D2 and D3 later carry
  74 and 75; D2 carries 12 where D3 carries 14; D3's as-of is a Sunday with 0 rows.
- Posting latency: 33 D2 rows dated inside D1's window are absent from D1 (30 within 7 days of D1's as-of,
  1 at 8-14 days, 2 at 31-49 days; 8 created after D1's as-of, 25 created on or before it). 15 D3 rows dated
  inside D2's window are absent from D2, all created after D2's as-of. 0 D2 rows inside D3's window are
  absent from D3. Rows are not final for about a week and occasionally seven.
- D1 is not sorted by `Date`; D2 and D3 are ascending. Occurrence numbering is unaffected (no collisions).

## Landed

- Header: 33 columns, identical in name and order across the three deliveries (0 added, 0 removed). No
  extra, missing-optional or prohibited columns. Physical lines after the header equal the csv record
  count and the report's landed count in every file: 17,089, 45,013, 4,198. 0 blank lines, every record
  33 cells, no footer. No BOM; D1 is CRLF, D2 and D3 LF.
- Filename pattern `weinfuse.*buybill` matches only the four BuyBill files. The reverse is a problem in the
  Administered spec: its pattern `weinfuse.*administereddrugs(?!.*buybill)` matches all four BuyBill files
  because `BuyBill` precedes `AdministeredDrugs` in the name, so the lookahead never sees it. The variant
  detector does keep them apart: BuyBill files fail Administered's legacy detector on `require_none: Vendor`
  and are reported UNRECOGNISED in that walk (3 files), never loaded. Correct outcome, noisy report.
- The April drop's `WeInfuse_AdministeredDrugsReport 01012026-04052026.csv` is byte-identical (same
  SHA-256, 1,483,951 bytes) to D3 and carries the BuyBill header (Vendor, Location Name; no Type). It is a
  BuyBill extract under the Administered name. Under this spec it binds to `legacy_export` with 4,198 rows
  and the same 4,198 row hashes, but the pattern does not route it here, and the Administered walk refuses
  it. Net effect: there is no April Administered extract at all.

## Typed

- 0 quarantined rows in any delivery; no spec change is needed for parsing.
- Format drift that typing hides: `Expiration` is US in D1 and ISO in D2/D3 (both declared, parses).
  `Strength` is written without a decimal on 17,070 D1 rows and with one in D2; `Unit Price` likewise on
  1,695 rows. Decimal canonicalisation in the row hash absorbs both (verified: 0 hash differences from them).
- Format drift that typing does not hide: `date` (US in D1, ISO in D2/D3) and `vendor_created_at` (`m/d/Y H:MM` in
  D1; ISO with seconds and a UTC offset in D2/D3, 2 distinct offsets = DST). Both are `type: text`, so the
  same value hashes differently. Would a spec change fix it: yes for `date` (retype to `date` with
  `['%Y-%m-%d', '%m/%d/%Y']`; parsed dates agree on 17,088 of 17,088 shared rows). For `vendor_created_at`, a
  `date` type with `['%Y-%m-%d %H:%M:%S %z', '%m/%d/%Y %H:%M']` would truncate to the date and agree on
  17,087 of 17,088, at the cost of the clock and offset; there is no datetime type. Not changed here.
- `time` is a datetime string whose date part is a single placeholder (1 distinct value per file, more than
  a year before every `Date`) with a timezone label; only the clock part carries information.
- NDC: every `Inner NDC` and `Outer NDC` is 11 plain digits, 0 blank, 0 hyphenated. Inner equals outer on
  14,646 / 38,654 / 3,684 rows.
- Money and quantities: `unit_price` is a bare positive decimal on every row (0 negative, 0 zero, 0 blank).
  `billing_units` is a non-negative integer; zero on 453 / 1,292 / 104 rows, of which 453 / 1,283 / 104 also
  have a blank `Billing Code (HCPCS)` (the only blanks in that column). 9 D2 rows have a HCPCS and zero units.
- `vendor_created_at` date part is later than `Date` on 759 / 3,861 / 75 rows and earlier on 3 / 3 / 0.
  `expiration` precedes `Date` on 16 D2 rows (0 in D1, D3).
- Control totals: no vendor footer; `billing_units` sums 818,485 / 2,346,719 / 188,011 (D1 / D2 / D3).

## Curated

- (order_id, inventory_item_id) is unique in every delivery: 17,089 / 45,013 / 4,198 distinct of as many
  rows; 0 within-file duplicate identities, max occurrence 1. Blank key cells: 0.
- Corrections D1 to D2: 17,088, differing columns `date` (17,088), `vendor_created_at` (17,088), `billing_units` (1),
  `lot` (1). Excluding `date` and `vendor_created_at`, 17,086 are hash-identical: format drift, not reorderings and
  not a wrong identity. The 2 real edits are one lot change (2025-10, same expiration) and one
  `billing_units` decrease (2025-03). D2 to D3: 1 correction, `vendor_created_at` only (moved 12 days, all other
  columns equal). 0 reorderings, 0 conflicts.
- The one identity present only in D1: dated the day before D1's as-of, created on or before the as-of; its
  order_id (48 rows in D2) and appointment_id (4 rows) persist, but its inventory_item_id never appears in
  D2 or D3. A line deleted or replaced after extraction: a genuine vanish, 40 billing units, current forever
  until the vanish rule exists.
- Late-file replay of D1: 17,088 late, 1 unchanged (the orphan), 0 current rows changed. Correct.
- Overlap with `dispense.weinfuse.administered`: every BuyBill key exists in the Administered file of the same
  drop (17,089 of 17,089 in D1; 45,013 of 45,013 in D2), and on matched rows `billing_units` and the date
  agree 45,013 of 45,013. Administered has 6,165 further rows (2023: 1,413; 2024: 2,011; 2025: 2,289;
  2026: 452) across 88 NDCs, 53 of whose 80 label names also occur in BuyBill. BuyBill is a strict subset,
  not a disjoint feed; both specs use the same natural key with the same values, so a union double counts.

## Presented

- 46,323 current rows = 45,013 (all of D2) + 1,309 (D3 rows D2 never saw) + 1 (the D1 orphan). Current
  `billing_units` 2,406,564 = 2,346,719 + 59,805 + 40, exactly. Current rows by year: 2023 12,299;
  2024 14,409; 2025 15,496; 2026 4,119. Nothing is counted twice; D1 contributes only the orphan.
- A month-partitioned union (newest delivery per month) does not reproduce the total, by 59,461, because D3
  holds only 80 of 1,362 December-2025 rows (the boundary day). Coverage must be reasoned per day, not month.

## Proposed event-date column

`date`. It is populated on every row, parses under two formats with 0 failures, never exceeds the delivery
as-of, and the vendor's own range filter is evidently applied to it (with the previous day included). It is
the same value as the Administered spec's `administration_date` on all 45,013 matched rows. `vendor_created_at`
is not it: it trails `Date` on up to 3,861 rows and is a load time, not an event. For the vanish rule, treat
a delivery's coverage as [filename start minus 1 day, as-of minus 1 day]: the first because the full
previous day is always present, the second because the as-of day is extracted part way through.

## Open questions for the developer

1. Declare `date` as the event-date column (field to be added to the spec model): yes/no?
2. Retype `date` from `text` to `date` with `['%Y-%m-%d', '%m/%d/%Y']` so D1-to-D2 drift stops producing
   17,086 cosmetic corrections: yes/no?
3. `vendor_created_at`: keep as text (cosmetic corrections continue), retype as `date` with datetime formats (loses
   clock and offset), or add a datetime type to the model. Named decision.
4. Should vanish coverage be [start minus 1 day, as-of minus 1 day] given the full previous day is included
   (19 of 19, 80 of 80) and the as-of day is partial (50 vs 74): yes/no?
5. Once the vanish rule exists, should the single D1-only identity be retired as vanished: yes/no?
6. Fix the Administered spec's pattern so the lookahead sees `BuyBill` (anchor it at the start), removing
   three UNRECOGNISED entries from that walk: yes/no? (Other spec; flagged, not changed.)
7. The April Administered-named file is a byte copy of the BuyBill file: re-request the true April
   Administered extract, or accept that the June xlsx (from 2026-03-01) is the only post-March source?
   Named decision.
8. Given BuyBill is a strict subset of Administered with identical keys, should pres expose it as a flag or
   join on Administered rather than as a second fact set: named decision.
9. Flag the 16 D2 rows whose lot expiration precedes the administration date for the business: yes/no?
10. Keep `lifecycle: discontinued`, or is a replacement buy-and-bill extract expected from the new reporting
    module: named decision.

## Probes run

- Parsed each CSV from raw bytes: BOM, line endings, physical lines after header, csv record count, blank records, record widths, header name/order across deliveries and against the three Administered CSVs.
- Classified raw `Date`, `Time`, `Created At`, `Expiration`, `Unit Price`, `Inner NDC`, `Outer NDC`, `Billing Units`, `Strength` cells by shape per file; counted blanks per column; counted distinct keys, orders, appointments, vendors, locations.
- Applied both specs' filename patterns to all seven WeInfuse administered-type filenames; loaded every file under both specs to see which variant binds or which detector refuses it; compared SHA-256 of the April pair.
- Loaded each file with the real loader; per file counted rows after as-of, before filename start, by month, by day at both boundaries, within 7 days of as-of, and Python types, negatives, zeros, HCPCS blanks, expiration-before-date, vendor_created_at-versus-date ordering.
- Keyed every file by (identity hash, occurrence); per pair counted shared identities, hash-identical ones, differing column names and sets, identities present on one side only inside the shared window with month and lag buckets, and whether their order or appointment ids exist on the other side.
- Recomputed row hashes excluding chosen columns to isolate which columns drive the D1-to-D2 differences; compared Decimal string forms versus values; compared parsed `vendor_created_at` wall clocks and date parts, and counted distinct UTC offsets.
- Counted `Time` distinct date parts and its lag from `Date`; for before-start rows checked day of month, shared order/series/appointment ids with in-window rows, and vendor_created_at lag.
- Replicated the dedup rule in memory; reconciled current rows and `billing_units` as D2 plus D3-only plus the D1 orphan; tried a month-partitioned union; counted current rows by year and by delivery.
- Compared BuyBill keys, order ids, appointment ids, item ids, `billing_units` and dates against the Administered file of the same drop; profiled the Administered-only rows by year, NDC and label-name overlap.
- Listed every drop folder for the presence of a BuyBill file.
