# Data findings

Empirical findings from the real Sagebrush extracts, established by running code
against the files rather than by reading the previous implementation. Each one is
either already closed by a test in this repository or is an open question for the
business.

Sources: `Sagebrush ETL Files\Original Files\` (54 vendor files across three drops,
plus `Monthly Payments.zip` and `Master COGS List.xlsx`).

---

## 1. Parenthesised negatives were silently discarding $39.97M

**Status: closed.** `transforms/values.py`, tests in `tests/unit/test_values.py`.

`Monthly Payments` writes negative amounts in accounting notation — `($40.00)` for
minus forty. The previous `clean_currency` stripped `$`, commas and whitespace but
not parentheses, so `float("(40.00)")` raised and the value became `NULL`.

Measured across all 40 monthly files:

| | |
|---|---|
| Rows | 1,287,742 |
| Rows with parenthesised amounts | 227,284 (17.6%) |
| Rows the old parser nulled | 227,284 |
| Correct signed total | $191,240,346.67 |
| Old parser total | $231,207,544.28 |
| **Overstatement** | **$39,967,197.61** |

The loss is directionally biased, not random: every parenthesised value is a credit,
adjustment or transfer, so dropping them inflates every total. A reconciliation built
on this could never have tied.

Money is `Decimal` end to end. Binary floating point cannot represent 0.01, and these
files carry over a million payment lines.

## 2. NDC normalization was wrong on 54.5% of distinct NDCs

**Status: closed.** `transforms/ndc.py`, tests in `tests/unit/test_ndc.py` and
`test_ndc_ground_truth.py`.

Both the previous SQL (`RIGHT('00000000000' + REPLACE(ndc,'-',''), 11)`) and Python
(`zfill(11)`) left-pad. That implements the FDA 4-4-2 rule and only that rule, so it
produces a different drug for every 5-3-2 and 5-4-1 NDC.

Validated against the McKesson Custom export, which carries the 10- and 11-digit forms
on the same row: new implementation 101/101 correct; old implementation wrong on 55,
inventing 55 NDC values that do not exist. Because the corrupted values look like
valid NDCs, they fail to join silently rather than raising.

This is a plausible contributor to the 4,715 Pharmaforce and 982 Wellpartner "orphan
NDCs" that the earlier validation pack attributed to alternate 340B supply channels.

An unhyphenated 10-digit NDC cannot be disambiguated from its digits alone, so it is
resolved against the NDC dimension and quarantined as `AMBIGUOUS` or `NOT_FOUND`
rather than guessed.

## 3. Cardinal ships two different reports and two date formats

**Status: closed in the spec model; see open question 3.**

Two genuinely different Cardinal reports are in the folders:

| File | Cols | NDC | Line # | Qty |
|---|---|---|---|---|
| `Cardinal_Invoice 01012023-03062026.xlsx` | 9 | no | no | no |
| `Cardinal_Invoice Review 01012023-04162026.csv` | 17 | yes | yes | yes |
| Jan–Apr and Mar–Jun 2026 drops | 17 | yes | yes | yes |

The 9-column invoice **summary** is what led to the earlier conclusion that "Cardinal
carries no NDC". The 17-column **detail** report does carry it, and the April 16th
pull covers the full history — 20,603 rows, NDC present on 20,602. No re-request to
Cardinal is needed; the ETL prefers the detail report and recognises the summary as a
separate variant by header fingerprint.

Two further facts about that file:

- Despite being named `01012023-…`, it contains **no 2023 data**. Invoice dates run
  2024 (14,165), 2025 (5,061), 2026 (1,376). Filenames overstate coverage and are not
  a reliable source of period metadata.
- The same report ships **two date formats across pulls**: `2024-09-16` in the April
  pull, `01/01/2025` in the February pull. A value like `01/02/2025` is genuinely
  ambiguous, and `pandas` inference resolves it from the rest of the column — so the
  same string can parse differently depending on what else is in the file. The
  previous `safe_date` declared a format parameter and never passed it. Formats are
  now declared per variant and a mismatch fails loudly.

## 4. The previous pipeline never ingested anything

**Status: context, no action.**

Across every logged run from 30 June to 21 July 2026, `promoted=0`. Sixteen of
eighteen sources never matched their glob pattern — logged as `WARNING` while the
WebJob reported `"Status":"Success"` every time. The two that matched were a duplicate
skip and the same 1,183 rows re-upserted. Everything in `pharmacy_qa` arrived via 19
hand-run scripts that bypass staging, validation, dedup and NDC normalization.

Consequence for this build: an expected-source manifest with freshness alerting is
required, and the run summary must count supersessions as well as inserts — the old
`rows_promoted` excluded MERGE updates, which is why working runs reported zero.

## 5. Schema variants are real and ongoing

**Status: closed in the spec model.**

| Source | Change |
|---|---|
| Cardinal | 9-col summary → 17-col detail |
| WeInfuse Administered | 33 cols CSV → 35 cols XLSX ("new reporting module") |
| WeInfuse Patients | CSV → XLSX |
| WeInfuse BuyBill | **discontinued** — "not in the new reporting module" |
| PRXP | 90 → 94 cols |

WeInfuse BuyBill has 45,013 historical rows and is the source the 340B verification
plan built its entire reconciliation on. That feed has stopped.

Sheet names vary in case (`Results`, `sheet1`, `Sheet1`). Header row varies: Athena
always row 1, ABC xlsx row 1, ABC csv row 2 (a `sep=,` line plus a title), everything
else row 0. File extension is not a reliable identifier of a source.

## 6. Master COGS List resolves account → site, but not by account number

**Status: partially closed; see open questions 1 and 2.**

`(Payee, Account No, Location)` is unique across all 121 Master Sheet rows. `Account
No` alone is **not** — McKesson `62766` maps to three different classes across five
rows. Any resolution keyed on account number alone will mis-attribute.

The file also supplies:

- The **channel split** in `Category`: `Medical Supplies (Buy and bill 340B (owned
  clinics))` (11 rows) versus `Medical Supplies (Prescription 340B (Owned & Community
  Outreach Clinics))` (92 rows). This is the distinction that prevents a buy-and-bill
  administration and a TPA claim being counted as two separate events — the
  double-subtraction risk in the old `purchased − dispensed − claimed` formula.
- The **CuraScript account→site map** (10 accounts), even though CuraScript purchase
  data is not ingested. `120090223` → South Carolina, `120089567` → Eastern
  Rheumatology, `120090224` → CT Southington.
- Confirmation of the shared-account problem: Cardinal `2057229358` is Southington-only,
  `2057234776` is `Unassigned` (shared).

Data-quality issues to normalize in the dimension while preserving raw values:
`PharmaForce` vs `Pharmaforce` (case), `Accerdo` (Accredo), `Wallgreens` (Walgreens),
`ROEMONT` (Rosemont), `PHARMA BUDIES` (Buddies). One row has an empty `Class_New`.
`Location` contains embedded newlines (multi-line ship-to addresses).

Note also that ABC account numbers appear here **without** a leading zero
(`100583154`) where the wholesaler extracts and the 340B verification plan use
`0100583154`. Leading zeros are significant in identity hashing, so account matching
needs an explicit normalization rule rather than an incidental one.

## 7. Monthly Payments is the highest-PHI file in the estate

**Status: policy set; see decisions.**

Per month, roughly 33,000 rows × 116 columns. Direct identifier population in the
June 2025 file:

| Column | Populated |
|---|---|
| LName, FName, DOB, PatStreet1, PatCity, PatState, PatZipCode, PatNum | 100% |
| CellPhone, Email | 99.9% |
| Phone | 89.9% |
| **SSN** | **69.1%** |
| MI | 38.1% |
| MRN | 35.7% |

Decision taken: **SSN is not landed at all.** It is never needed to reconcile a
payment. Remaining direct identifiers land encrypted at rest with column-level access
control and an access audit log; `PatNum` and `MRN` stay in clear as join keys.

Other structure: three date formats in one file (`TransDate` `6/17/2025`, `TransMonth`
`Jun 2025`, `PostMonth` `Oct 2025`). `PostMonth` can be months after `TransMonth`,
which matters for period assignment. Codes carry trailing whitespace (`'UNASGN  '`).
`IsVoided` is `True`/`False` strings with a separate `VoidSign` of `1`/`-1`. Header is
stable across all 40 files — one variant. The last file uses a different naming
convention: `Monthly Payments 2026-07 upto 07212026.csv`.

Two months are outliers worth confirming: `2024-01` has 118 lines and `2023-04` has
2,282, against a norm of roughly 30,000.

---

## Open questions

1. **When did `Class` → `Class_New` take effect?** 28 of 121 accounts were
   reclassified, mostly `Unassigned` → `CVS (Wellpartner)` / `PharmaForce` / `PRXP`.
   Decision taken: land both columns without interpreting them. Consequence: entity
   resolution reports `UNRESOLVED` for those 28 accounts until a date is supplied.
   Everything else resolves normally.

2. **Master Sheet versus per-vendor tabs.** Decision taken: Master Sheet is
   authoritative. Five rows exist in a vendor tab but not in Master, and they include
   a genuine disagreement rather than a mere omission — McKesson `79926915` is
   `Fire Mesa ID` in Master and `Fire Mesa Rheumatology` in the McKesson tab. These
   are reported as data-quality exceptions for finance to reconcile in their own
   workbook.

3. **Which Cardinal report is the ongoing feed?** The detail report is clearly the one
   to reconcile against. Worth confirming that future pulls will use it consistently,
   and in a single date format.

4. **Is `Monthly Payments` the right source for financial reconciliation?** Its columns
   (`Voucher_Number`, `Service_ID`, `IsVoided`, `VisitLocAbbr`, `ProcCode`, `Fee`)
   overlap heavily with the existing `Curve_Monthly Services` extract. They appear to
   be transactions and charges from the same practice-management system. Confirm
   whether both are needed or one supersedes the other.
