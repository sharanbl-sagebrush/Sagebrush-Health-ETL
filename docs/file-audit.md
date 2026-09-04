# File audit: every extract checked against the extraction logic

A structural audit of all 59 vendor files across the three delivery drops, plus
the Master COGS List and the 40 members of `Monthly Payments.zip` — 67 file and
worksheet audits in total. The question it answers is narrow and specific: **is
there anything in a file's shape that the reader and the spec model do not cope
with?**

Reproduce with `python scripts/audit_files.py`. It reads the head and tail of
each file, so a 260 MB extract costs about the same as a small one, and it
examines values without ever printing them.

## What it checks, and why each one matters

| Check | The failure it prevents |
|---|---|
| Preamble lines | A report title read as a header, so every column binds to nothing |
| Header row | Duplicate or blank column names that make a binding ambiguous |
| Trailing footer | A totals row landing as a phantom transaction |
| Ragged rows | Truncated rows, or an embedded newline splitting one row into two |
| Embedded newlines | Multi-line cells breaking a naive line-based reader |
| Encoding / BOM | Mojibake in identifiers, which then fail to join |
| Workbook structure | Merged header cells, hidden sheets, used ranges past the data |

## Result

Every file is handled. Two real changes came out of it, both now implemented.

### 1. A second totals footer — McKesson Purchasing Detail

Only two files in the estate carry a footer, and the audit found the one I had
not seen:

| File | Footer |
|---|---|
| `Cardinal_Invoice Review` | 3 of 17 cells at positions 14–16 |
| `McKesson_Purchasing Detail - Invoice Level` | **11 of 45 cells at positions 34–44** |

The McKesson footer (line 6165) declares `Extended Price ($) = $20,394,456.72`,
`Net Quantity = 11,378`, `Ordered Quantity = 14,155`, `Gross Purchases ($) =
$20,479,715.99` and `Gross Credits and Returns ($) = -$85,259.27`. Those figures
are the source of the "$20.39M extended price, 11,378 net units" line in the
earlier validation pack — it was reading the vendor's footer, not the data.

Landing it would add a phantom $20.4M purchase. Skipping it silently would waste
a free control total. It is declared as `footer: totals_row`, and the declared
figures are checked against ours.

`McKesson_Custom Report` was checked too and has **no** footer — its last row is
real data. The two McKesson reports differ here, which is exactly the kind of
thing that gets assumed rather than checked.

### 2. ABC's preamble is not a fixed length

ABC delivers the same 26 columns two ways:

```
XLSX, sheet "Results"          CSV
  row 0  report title            line 0  sep=,
  row 1  header                  line 1  report title
                                 line 2  header
```

One title row as a workbook, two lines as a CSV. A fixed `preamble_lines` cannot
serve both, and two variants cannot be told apart because their headers are
identical — that combination would have been an `AmbiguousVariantError` on every
ABC delivery.

The fix is to stop counting junk lines and instead declare what identifies the
header: `header_marker: "Account #"`. The reader scans up to
`max_preamble_scan` rows for the row carrying that column. This stays
declarative — a human wrote down what the header looks like — and a missing
marker raises `HeaderNotFoundError` rather than falling back to "probably row
0", which is how a report title becomes a header.

## Per-source structural facts

| Source | Header row | Preamble | Footer | Notes |
|---|---|---|---|---|
| ABC Purchases | varies | 1 (xlsx) / 2 (csv) | no | sheet `Results`; account `0100461951` — leading zero significant |
| Cardinal Invoice (detail) | 0 | none | **yes** | two date formats across pulls |
| Cardinal Invoice (summary) | 0 | none | no | superseded, `recognise_only` |
| McKesson Purchasing Detail | 0 | none | **yes** | unnamed index column at position 0 |
| McKesson Custom | 0 | none | no | unnamed index column; two NDC columns |
| WeInfuse Administered | 0 | none | no | 33 cols CSV → 35 cols XLSX, 14 renamed |
| WeInfuse BuyBill | 0 | none | no | discontinued after the module change |
| WeInfuse Patients | 0 | none | no | CSV → XLSX |
| Allscripts eRx | 0 | none | no | UTF-8 BOM |
| Curve Appointments | 0 | none | no | 143 MB, 101 cols |
| Curve Monthly Services | 0 | none | no | 260 MB, 122 cols, `Textbox*` artifacts |
| Pharmaforce Claims | 0 | none | no | one spec, two sites via constants |
| PRXP | 0 | none | no | 90 → 94 cols across drops |
| Wellpartner Claims | 0 | none | no | 119 cols; `Days` Supply` header typo |
| Wellpartner Invoices | 0 | none | no | 10 cols |
| Athena Encounters | **1** | 1 | no | `REPORT NAME :` title row |
| Athena Prescriptions | **1** | 1 | no | title row padded with commas |
| Athena Eligibility | **1** | 1 | no | title row padded with commas |
| Master COGS List | 0 | none | no | 6 sheets; embedded newlines in `Location` |
| Monthly Payments | 0 | none | no | 40 files, one stable 116-col header |

## Findings that turned out not to be problems

Worth recording, because each one looked like a defect and cost time to clear.

**Unnamed column at position 0** — McKesson Custom, McKesson Purchasing Detail
and both new-module WeInfuse workbooks open with an unnamed row-number column.
It is an export artifact, already excluded from matching by `is_junk_header`,
and it carries no data any spec needs. Verified, not assumed.

**"Rows with more fields than the header"** on the Master COGS List — the
worksheet's used range runs to column K while the header stops at G. Columns
H–K are empty on all 121 rows. The reader trims trailing empty header cells, so
it reads seven columns correctly. This was an audit artifact, and the audit now
trims before comparing.

**Ragged rows in Monthly Payments** — the first audit reported rows with 38 and
7 fields against a 116-column header. That was a bug in the audit, not the data:
seeking into the middle of a large file to read its tail lands mid-line, and the
fragment counts as a short row. A full scan of every row of all 40 files
confirms the only non-conforming row in each is a single trailing blank line.
1.29 million rows, no truncation, no embedded-newline splits.

**Embedded newlines** — real, in the Master COGS List `Location` column, which
holds multi-line postal addresses. Handled: the CSV reader is quote-aware and
the value is preserved intact.

## What the audit does not cover

- **Merged header cells** are only detected on workbooks under 6 MB. Larger ones
  are streamed, because loading a 25 MB workbook in full-inspection mode takes
  minutes. None of the small workbooks have merges; the large ones are
  unverified on this point.
- One `Reports for Download.xlsx` could not be opened — it is a OneDrive
  cloud-only placeholder. It is a delivery catalogue, not a data source.
- Raggedness on non-payment files is measured on a 5,000-row head sample rather
  than every row. Run with `--full` to scan everything.
