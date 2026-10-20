# Review: wholesaler.abc.purchases

Reviewed 2026-10-15 against `docs/layer-walk/wholesaler.abc.purchases.md` (generated the same day) and `specs/wholesaler.abc.purchases.yaml`. Counts, column names, dates and file names only; no cell values.

## Verdict

Safe to load with the listed changes. All four distinct deliveries (one CSV, three xlsx; a fifth xlsx is a byte-identical copy of the 2024-01-01..2026-03-06 file in a second folder and is correctly refused) bind to the one variant, land with the exact physical line count, type with zero quarantine, and curate without conflicts; the late-file replay moves nothing and the presented totals equal the newest-delivery-per-month union with no double counting (naive sum of the four deliveries would overstate invoice_ext_amt by 6,217,066.91). Three things need a decision before the commit engine runs this source: (1) the xlsx "creation stamp" on two files is an Excel Online re-save by a person, not the SAP export date, so as-of should come from the filename end date for ABC; (2) every one of the 934 "corrections" between deliveries is a change in a reference-price column only (awp, current_acq_cost, awp_date), which the row hash currently treats as a vendor correction; (3) the natural key is not unique on NDC-less service lines, and adding `abc` makes it unique in every file. None of these changes any current-row control total today, so the load can proceed while they are decided, but the as-of rule should be settled first because it is the only one that could change which delivery wins in future.

## Deliveries and as-of

- Four distinct deliveries, in as-of order: CSV Jan012025_Feb042026 (as-of 2026-02-04, filename), xlsx 01012024-03062026 (2026-03-29, xlsx stamp), xlsx 01012026-04052026 (2026-04-30, xlsx stamp), xlsx 03012026-06102026 (2026-06-10, filename). A fifth file, byte-identical to the 01012024-03062026 xlsx, sits under a second folder and is reported as a duplicate; it never lands twice.
- The April 5 workbook: confirmed `dcterms:created` 2026-04-30T00:15:16Z, and `dcterms:modified` is the identical instant. `dc:creator` is SAP WebAS but `cp:lastModifiedBy` is a person and the application is Microsoft Excel Online 16.0300. The March 6 workbook has the same shape (created = modified 2026-03-29, a different person, Excel Online). The June 10 workbook, which nobody re-saved, has application Microsoft Excel 12.0000 and carries no created or modified stamp at all. So the SAP export does not write a stamp; the stamps the walk trusted were written by Excel Online when a person opened and saved the file, 25 and 23 days after the data ends.
- Every file's data stays within its filename range: max invoice_date per file is 2026-02-04, 2026-03-06, 2026-04-04 and 2026-06-10 against filename end dates 02-04, 03-06, 04-05, 06-10; zero rows beyond the filename end, zero rows before the filename start, zero rows beyond the as-of. The filename end date is therefore the extraction bound; the stamp is a later re-save.
- Ordering: by stamp or by filename the four deliveries sort the same way today, so the walk's outcomes are unaffected. The rule is still fragile for ABC: an old workbook re-saved after a newer extract would carry a newer stamp and win. Recommendation: for this source take as-of from the filename end date, or treat a stamp as a vendor stamp only when `dcterms:created` differs from `dcterms:modified` or the application is not Excel Online. This is a `layer_walk.py` / future engine rule, not a spec field.
- Event date: `invoice_date` (see the section below). `awp_date` is a reference-price date (minimum 2000-10-04, null exactly on the NDC-less rows) and is not an event.

## Landed

- CSV: 1,229 physical lines = `sep=,` line + title + header + 1,226 data lines; CRLF, no BOM, trailing newline, every data line has 26 fields, no blank lines. Landed count 1,226 matches.
- xlsx: sheet "Results" only; 636 / 609 / 4,515 rows = one title row + header at row 2 + 634 / 607 / 4,513 data rows; max_column 26, no blank rows. Landed counts match.
- Both CSV and xlsx bind to variant `invoice_detail` with all 26 targets present, zero extra columns, zero missing optional columns, zero prohibited columns. The bound target sequence is identical across all four deliveries: no header drift.

## Typed

- Zero quarantined rows in any delivery; counters balance everywhere. No spec change is needed for quarantine.
- Dates: `Invoice Date` and `AWP Date` are mm/dd/yyyy text in the CSV and also text cells (not Excel dates) in every xlsx, so the first declared format matches every row and the ISO fallback is never exercised. No wrong-format parsing possible today.
- NDC: every populated NDC is already 11 digits unhyphenated in both file forms (1,128 / 591 / 565 / 4,146); blanks are 98 / 43 / 42 / 367 and coincide exactly with the `awp_date` nulls. The spec's "367 of 4,513" claim is confirmed.
- Money: CSV negatives use a leading minus (no parentheses, no `$`, no thousands separators); xlsx cells are numeric. Negative invoice_ext_amt rows: 136 / 66 / 83 / 202, with shipped_qty negative on 141 / 67 / 84 / 224 rows - credits and returns, as expected.
- Footer: none; ABC ships no totals, so there is no vendor claim to check. Control totals are the ETL's own figures.
- Control-column relationship, every file: `invoice_ext_amt == total_ext_cost + tax_amount - non_merchandise` on all rows except 5 in the 2024-2026 file. On every non-merchandise row (33 / 7 / 7 / 76) invoice_ext_amt is zero and the charge sits in total_ext_cost with the sign of non_merchandise; net non_merchandise is negative in every file (3 / 1 / 1 / 19 large credit rows outweigh 30 / 6 / 6 / 57 small charges). So the two dollar control totals differ by design: invoice_ext_amt excludes service charges, total_ext_cost includes them. All 367 NDC-less rows carry a non-zero shipped_qty, so the shipped_qty control total includes service lines that are not packages.
- Spec change proposal (not applied): none required for typing. Worth noting that `non_merchandise` is additive dollars and could be a control_total so the service-charge figure is recorded on the batch.

## Curated

- Within-file duplicate identities: 1 / 0 / 1 / 2 groups, each of exactly 2 rows, none byte-identical. All four are NDC-less service lines on the same account and invoice, adjacent in the file (line gap 1), with different `abc`, `product_description` and `customer_po`/`order_qty`/`current_acq_cost`; both rows carry zero invoice_ext_amt. They are real distinct lines, not repeats, and the key is too narrow only because `ndc` is null on them. Adding `abc` to the natural key gives zero duplicate groups in every file (`abc` is never null in any delivery); adding `customer_po` alone leaves 1. Occurrence numbering preserves both lines, and the one duplicate identity that appears in more than one delivery has the same `abc`/`product_description` at occurrence 1 and 2 in each, so the walk's "reorder only" count of 0 is genuine.
- The spec `notes` still say identity "falls back to the row hash"; under the current rule it is key + occurrence. The comment is stale.
- Corrections: 545 (CSV superseded by the 2024-2026 xlsx), 274 (by the Jan-Apr xlsx), 115 (by the Mar-Jun xlsx), total 934. Columns differing, with row counts: `awp` 369 / 2 / 0, `current_acq_cost` 175 / 274 / 115, `awp_date` 20 / 2 / 0. No other column differs in any correction; zero corrections touch a control column, the key, or any business field. These are reference prices re-looked-up at extraction time, not vendor corrections. The row hash uses every landed column (`row_hash_fields` exists in `ingest.load` but is always `None` and is not exposed by the spec model), so every future re-extraction will "correct" every row whose AWP or acquisition cost moved.
- Late-file replay of the CSV after everything else: late 627, unchanged 599, inserted 0, conflict 0, current rows changed 0. Passes.
- Vanish exposure, measured with `invoice_date`: for every (older, newer) pair, zero identities in the older delivery are absent from the newer one within the newer file's actual date span or its filename span. A vanish rule on this source would supersede nothing today.

## Presented

- Current rows 5,076; invoice_ext_amt 18,253,091.91; total_ext_cost 18,185,955.72; shipped_qty 20,481.
- Month-by-month, the current sum of invoice_ext_amt equals the sum taken from the newest delivery covering each month (both 18,253,091.91), and every month covered by more than one delivery agrees across those deliveries where their ranges fully overlap (the only differences are partial first/last months of a range). Naive sum of the four deliveries is 24,470,158.82; no double counting across the overlapping ranges.
- Data-shape flag for the business, not the ETL: Feb-May 2025 hold 2 / 2 / 3 / 3 rows with zero dollars, between 106 rows in Jan 2025 and 240 in Jul 2025; both deliveries that cover 2025 agree, so it is what ABC shipped.

## Proposed event-date column

`invoice_date`. It is required, populated on all 6,980 typed rows, parsed from one format everywhere, and in every delivery its maximum equals the filename end date (within one day) and its minimum stays at or after the filename start, so it is the date the vendor's range filter runs on. A vanish check keyed on it has zero exposure across all six delivery pairs. `awp_date` is the only other date and is a price-reference date going back to 2000.

## Open questions for the developer

1. Should ABC's as-of come from the filename end date rather than the xlsx stamp, given the stamp is an Excel Online re-save date (created == modified, lastModifiedBy a person) and the untouched SAP export has no stamp? Yes/no, or name the rule (for example "trust a stamp only when created differs from modified").
2. Should `awp`, `current_acq_cost` and `awp_date` (and the other rate columns: `package_awp`, `fdb_*`, `customer_pack_size`, `unit_size_*`) be excluded from the row hash via `row_hash_fields`, so re-extractions with refreshed reference prices are "unchanged" rather than 934 corrections? Yes (expose it in the spec model) / no (accept rate-only corrections as history).
3. Should `abc` be added to `natural_key` ([account, invoice, ndc, abc]), which makes identity unique in every file without changing any outcome today? Yes/no.
4. Should the spec `notes` paragraph about identity "falling back to the row hash" be rewritten to describe key + occurrence? Yes/no.
5. Which dollar column is "purchases" for reconciliation: `invoice_ext_amt` (excludes service charges) or `total_ext_cost` (includes them, net negative)? Named decision.
6. Should `non_merchandise` become a control_total, and should reconciliation exclude the 367 NDC-less rows from `shipped_qty`? Yes/no each.
7. Should the vanish rule be enabled for ABC on `invoice_date` once the engine supports it, given zero exposure today? Yes/no.

## Probes run

- Listed every file matching the spec pattern with its folder, size, worksheet names and `docProps/core.xml` / `app.xml` creator, application, created and modified stamps (file #5 duplicate located; stamp provenance established).
- Per delivery via the real loader: min/max `invoice_date` and `awp_date`, rows outside the filename range, rows after the as-of, row counts by month.
- Physical structure: CSV line count, `sep=` line, header line number, CRLF/BOM, field-count distribution, blank rows; xlsx sheet names, row count, header row, max column, blank rows.
- Header drift: bound target sequence, new and missing-optional columns, compared across all four deliveries.
- Raw shapes: date text patterns, NDC shapes (hyphenated / 10 / 11 digit / blank), money sign notation, Excel cell types, account/invoice lengths and leading zeros.
- Typed signs and nulls: negative counts per control column, NDC nulls versus non-merchandise and zero-amount rows, NDC length distribution.
- Within-file duplicate identity groups: size, NDC null, line gap, differing column names, sign agreement; re-keyed with `customer_po`, `product_description`, `abc` to count remaining groups.
- Dedup replay in as-of order recording, per correction, the set of differing typed columns and whether any control column was involved.
- Occurrence-order stability of duplicate pairs across deliveries by comparing `abc`/`product_description` at each occurrence.
- Presented union: current invoice_ext_amt by month versus each delivery's monthly sum, naive four-delivery sum, newest-delivery-per-month sum.
- Vanish exposure: for each older/newer pair, older identities absent from the newer delivery within the newer file's actual and filename date spans.
- Control-column identity: invoice_ext_amt versus total_ext_cost + tax_amount +/- non_merchandise, non-merchandise sign counts, shipped_qty on NDC-less rows.
- Grep for `row_hash_fields` in `src/` and `specs/` to see whether the spec can exclude columns from the row hash.
