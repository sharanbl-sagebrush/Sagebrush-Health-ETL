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

## 8. The two McKesson reports are the same purchases, and would double-count

**Status: confirmed numerically.**

Loading `McKesson_Purchasing Detail - Invoice Level` and `McKesson_Custom Report`
independently, from separate files through separate specs, produces:

| Measure | Purchasing Detail | Custom Report | |
|---|---:|---:|---|
| Extended price | $20,394,456.72 | $20,394,456.72 | identical |
| Gross purchases | $20,479,715.99 | $20,479,715.99 | identical |
| Net quantity | 11,378 | 11,378 | identical |
| Ordered quantity | 14,155 | 14,132 | **differs by 23** |

The earlier validation pack observed that the two "represent the same underlying
purchases viewed two ways" and warned that using both would double-count. That
is now confirmed from the data rather than inferred: three of four control
totals agree to the cent.

The Purchasing Detail figures above are the vendor's own footer, so the
agreement is three-way — McKesson's arithmetic on one report, our arithmetic on
that report, and our arithmetic on a different report.

Open: the 23-unit difference in ordered quantity. Small, but the two reports
agree exactly everywhere else, so it is a real difference rather than rounding.
Worth asking McKesson which report is authoritative for ordered quantity, and
worth deciding which of the two is the purchase source before reconciliation is
built — loading both as purchases would double every McKesson figure.

## 9. Non-drug invoice lines exist and must not be discarded

**Status: closed in the specs.**

367 of 4,513 rows in the ABC extract carry no NDC. They are service charges —
`PHARM DATA SERVICES PRICE MAINTENANCE` with a Non-Merchandise amount and a zero
extended amount. 63 rows of McKesson Custom are the same shape.

They are real invoice lines carrying real dollars, so they belong in the
warehouse for invoice-level financial reconciliation even though they cannot
take part in NDC-level inventory reconciliation. `ndc` is therefore not a
required column on either source.

This was found by running the spec against the real file: the first version
marked `ndc` required and quarantined all 367. It is the reason the spec
scaffolder emits everything as `required: false` — requiredness is a claim about
the data that has to be checked against the data.

---

## 10. Three "unknown" natural keys, resolved

**Status: closed in the specs.**

The earlier validation pack listed five sources whose business key it could not
find. Three of them are WeInfuse, and all three are `(Order ID, Inventory Item
ID)` or a subset:

| Source | Pack's finding | Actual key | Verified against |
|---|---|---|---|
| WeInfuse Administered | "best returned 25,738 duplicates of 51,178" | `(order_id, inventory_item_id)` | 51,178 rows, unique |
| WeInfuse BuyBill | "2 candidates, both 22,173 duplicates of 45,013" | `(order_id, inventory_item_id)` | 45,013 rows, unique |
| WeInfuse Patients | declared `UQ_weinfuse_patient` | `patient_identifier` | 1,918 rows, unique |

The pack had been testing `Weinfuse UID + Appointment ID`. **`Inventory Item ID`
is the discriminator** — one appointment administers several inventory items, so
any key without it collides by construction. `(Appointment ID, Inventory Item
ID)` is equally unique; `Order ID` is preferred as the more granular of the two.

Still open from that list: Allscripts eRx and Athena Encounters/Prescriptions.

## 11. WeInfuse writes two temporal shapes in one file

**Status: closed in the spec.**

The Patients export carries plain ISO dates for anything a human entered
(`Date of Birth`, `Primary Insurance Effective Date` — `1960-02-22`) and UTC
timestamps for anything the system stamped (`Last Updated`, `Primary Insurance
Entered At` — `2025-02-14 17:13:35 UTC`).

Declaring the wrong one quarantined all 1,918 rows on the first run of the spec.
That is the intended behaviour: a declared format that no longer matches must
stop the load rather than be reinterpreted. Under the previous implementation,
which passed no format at all, `pandas` would have parsed both and nobody would
have known which interpretation it chose.

## 12. The new WeInfuse module drops Location ID from the patient report

**Status: open — worth raising with WeInfuse.**

The Patients report went from 48 columns to 23 in the new reporting module.
Lost: `Weinfuse UID`, separate first and last name, `Sex`, `Ethnicity`, `Race`,
`Preferred Language`, all insurance detail beyond a plan name, `Group ID` — and
`Location ID`.

`Location ID` is the site key. The 340B verification plan depends on it to
attribute activity to a clinic, precisely because shared wholesaler accounts
cannot: ABC `0100583154` serves all three clinics and Cardinal `2057234776`
serves two. The new patient report carries only a location *name*.

The administered-drugs export still carries `Location ID`, so dispense-level
attribution survives. But if the patient report is ever the only source of a
patient's site, that link degrades from an identifier to a name match.

---

## 13. Natural keys for every source, tested against the data

**Status: reopened 2026-10-16 by the layer walk, two rows corrected.** Every
key below was probed against the full 2023–2026 extract, not sampled, but two
probes answered the wrong question: uniqueness within one file, not identity
across re-deliveries. The layer walk (`docs/layer-walk/`) replayed every
delivery of every source through the dedup rule and found that Monthly
Payments' four-column key is not unique and that Curve Monthly Services does
have a key once `VoidSign` is included. Both specs were corrected the same day.

The earlier validation pack listed five sources as "candidate key unknown". Two
had keys nobody had found; three genuinely have none, and that turns out to be a
fact about the reports rather than a gap in the analysis.

### Sources with a real key

| Source | Key | Rows tested |
|---|---|---:|
| Cardinal invoice detail | `account, invoice, invoice_line` | 20,602 |
| McKesson Purchasing Detail | `invoice_credit_number, mckesson_item_number` | 6,163 - **not unique, see finding 16** |
| McKesson Custom | `invoice_credit_number, mckesson_item_number, po_number` | 6,165 - **not unique, see finding 16** |
| **WeInfuse Administered** | **`order_id, inventory_item_id`** | 51,178 |
| **WeInfuse BuyBill** | **`order_id, inventory_item_id`** | 45,013 |
| **WeInfuse Patients** | **`patient_identifier`** | 1,918 |
| Pharmaforce claims | `claim_id` — unique across *both* site files | 196,187 |
| PRXP | `rx_transaction_id` | 5,133 |
| Wellpartner claims | `transaction` | 42,018 |
| Wellpartner invoices | `invoice, wholesaler, account, date` | 7,700 |
| **Athena eligibility** | **`patientid`** | 23,188 |
| Master COGS List | `source_sheet, payee, account_number, location` | 121 |
| Monthly Payments | `clientid, voucher_number, service_id, seqnum, voidsign` | 1,287,742 (corrected: the four-column key had 181,688 within-month groups and 14,956 cross-month void reversals) |
| Curve Monthly Services | `service_id, voidsign` | 230,192 (corrected: the 5,803 residual pairs are voided charges carried with VoidSign +1 and -1) |

The three WeInfuse answers were the pack's open items. `Inventory Item ID` is
the discriminator — one appointment administers several inventory items, so any
key without it collides by construction. The pack had been testing
`Weinfuse UID + Appointment ID`.

### Sources with no key, and why

| Source | Best candidate | Residual duplicates |
|---|---|---:|
| Allscripts eRx | `imreenc_code, imremed_code, refill` | 14,325 of 132,974 |
| Curve Appointments | `patient_number, appointment_datetime` | 11,385 of 199,591 |
| Athena Encounters | `patientid, enc_date, dprtmnt, provdr` | 1,092 of 18,574 |
| Athena Prescriptions | `cln_enc_id, order_id, icd10_code` | 74 of 28,018 |

**Athena Prescriptions explains the whole category.** One prescription order
appears once per ICD-10 diagnosis linked to it — order 1861 on encounter 432
arrives twice, identical except `Z113` against `Z206`. The grain of the report is
order-by-diagnosis, not order. These are denormalised report extracts, not
transaction logs, and no subset of their columns identifies a row because the
vendor never intended one to.

Row identity therefore falls back to the row hash, which is sound for four of
the five: they contain no exact-duplicate rows. **Allscripts eRx is the
exception** — 8,100 of its 132,974 rows are identical across all 21 columns, so
even a row hash cannot separate them. That source needs snapshot semantics at
ledger time: a delivery replaces its date range rather than merging into it.

A spec that declares no natural key is now required to record which candidates
were tested and why none is unique. It is a finding about the data, and it
should read like one.

### Worth requesting from the vendors

- **Allscripts**: an order identifier. Would remove the duplicate problem outright.
- **Curve Appointments**: an appointment id. Curve holds one internally — Monthly
  Services and Monthly Payments both reference vouchers and services by id — so
  this is a reporting omission rather than a modelling one.
- **Curve Monthly Services**: no longer needed. `(service_id, voidsign)` is
  unique in every delivery (corrected 2026-10-16).
- **Athena Encounters**: the encounter id. The Prescription report already
  exposes it as `cln enc id`.

## 14. Curve Monthly Services carries SSN

**Status: closed in the spec.**

Not just Monthly Payments. `Curve_Monthly Services` also carries `SSN`, along
with name, date of birth, address and three telephone numbers — 122 columns and
230,192 rows the previous pipeline never loaded at all.

Classified `prohibited`, so the column is matched (drift and column-count checks
still see it) and then discarded before any value reaches the raw layer,
quarantine or a log.

Curve Appointments carries patient *and guarantor* names, addresses, dates of
birth, phones and email, though no SSN.

---

## 15. Typing columns by name misses real columns; typing by value does not

**Status: closed in how specs are authored.**

While writing the Curve specs I typed date columns with a name rule — anything
ending in `date`, plus a short list. Against `Curve_Monthly Services` that
matched three columns and **missed seven**:

| Missed | Why the name rule failed |
|---|---|
| `DOB` | does not contain "date" |
| `SvcDateFrom`, `SvcDateTo` | do not *end* in "date" |
| `BillDateOrig`, `BillDateInit` | same |
| `PIF_4`, `PIF_5` | names say nothing at all; both hold dates |

Seven real date columns would have landed as text. Nothing would have failed —
which is precisely the problem, because a date stored as a string does not
announce itself and is only discovered when a reconciliation joins on it and
quietly matches nothing.

Specs are now typed by sampling the column's actual values and detecting a
format that at least 98% of them parse under. The threshold matters: a lower one
mistypes a text column that happens to contain a few date-like values, and then
quarantines every other row in it.

Two columns in Curve Appointments — `DateTime_Bumped` and `CM_Date_Of_Injury` —
are empty in every delivery so far, so no format is detectable from values.
Those are typed by hand to match their siblings, with a note saying why. An
undetectable column is a decision for a person, not a default.

The same pass caught `Check_In_Time` and `Check_Out_Time`, which a name rule
reads as timestamps and which are in fact bare `HHMM` integers — `1454` for
14:54, with no date part. They are kept as text, because resolving them to a
timestamp means assuming which day they belong to.

---

## 16. Neither McKesson key is unique, and no column in the file completes it

**Status: decided 2026-10-15 - key plus occurrence number** (candidate 2 below),
as the single rule for every source; see `docs/architecture-and-plan.md`,
"Duplicates and delivery order". Probed 2026-10-07 against all three deliveries
of both reports; the probe is the ad hoc script described below and is not yet
in the repository. A further probe on 2026-10-15 confirmed that credits and
returns never collide with the purchase line they reverse: all 31 credit rows
in the history file carry their own credit number and none shares an
invoice-plus-item key with a purchase row, so a return is inserted beside the
original line rather than replacing it.

Finding 13 recorded both McKesson keys as verified unique. Re-running the check
shows they are not:

| Report | Key | Rows | Duplicate rows |
|---|---|---:|---:|
| Purchasing Detail | invoice, item | 6,163 | 11 |
| Purchasing Detail | invoice, item, original invoice | 6,163 | 2 |
| Custom | invoice, item | 6,165 | 13 |
| Custom | invoice, item, PO | 6,165 | 4 |

The residual collisions are split-shipment lines: the same item on the same
invoice, once with ordered 36 and filled 18, once with ordered 18 and filled 0.
They are identical in every identifier and differ only in quantities and
prices. The small 2026 deliveries show the same shape (5 and 7 duplicate pairs
on the two-column key, all resolved by the original-invoice reference).

Columns that do not help, and why:

- `ndc_upc_current` - every item number maps to exactly one NDC (0 of 427 items
  map to more than one), so it adds nothing. It is also blank on 63 service
  lines.
- `account_number` - every invoice belongs to exactly one account (0 of 3,275
  invoices span two), so it adds nothing.
- quantities and prices - they separate the rows but are the measures, so a
  corrected quantity would arrive as a new line rather than a correction.

Consequence: under the natural-key policy the second of two colliding rows would
supersede the first inside one file and a real purchase line would vanish. Under
the layered design this is a curated-layer rule and the row survives in landed
and typed regardless. The two candidates, with the same answer to be applied to
both reports:

1. **Row hash.** Every line survives today (none are byte-identical). A later
   vendor correction lands as a second live row rather than superseding.
2. **Key plus occurrence number.** Rows sharing a key within one delivery are
   numbered in file order and the number joins the key. Keeps correction
   semantics for every non-split line; depends on McKesson emitting split
   lines in a stable order, which the three deliveries on disk can test.

Asking McKesson for a line identifier is not an option.

---

## 17. Monthly Payments is the posting ledger of Curve Monthly Services

Asked 2026-10-23 by the developer: the two reports look alike, what is the
difference. Measured in `sbh_data_bank` on the current rows of both, counts only.

The two share 68 columns: the charge line (`voucher_number`, `service_id`,
`voidsign`, `svcdate`, `proccode`, `fee`, `units`, the provider, carrier,
department and location columns) and the patient demographics. What each has
that the other lacks says what each is:

- **Only Monthly Payments** (48 columns): `seqnum`, `transtype`, `transcode`,
  `transdesc`, `transcat`, `amount`, `date_paid`, `transdate`, `transmonth`,
  the remittance carrier columns and the reimbursement category columns. These
  describe a *posting*.
- **Only Curve Monthly Services** (46 columns): `claimnum`, `totpmt`, `totadj`,
  `totref`, `billdate`, the primary, secondary and tertiary carrier columns,
  `encounter`, the appointment columns, `pif_1` to `pif_5`. These describe a
  *charge line and its claim*.

| | Monthly Payments | Curve Monthly Services |
|---|---|---|
| current rows | 1,287,742 | 274,476 |
| distinct `service_id` | 249,532 | 268,434 |
| `service_id` present in both | 247,212 | 247,212 |
| payment rows whose service line is in Services | 1,283,114 (99.6%) | |

A service line carries two to twelve payment rows (one row on 6,779 lines,
two on 47,598, up to twelve on 2,688). By `transtype`: 181,422 `I` and
181,422 `O` rows, identical counts with identical `transdesc` values
("Medicare Transfer", "Commercial Insurance Transfer"), which is a transfer
out of one responsible party and into the next; 558,243 `P` payments;
365,499 `A` adjustments; 104 `R` refunds; 1,052 `M`. The sum of a service
line's `P` rows equals the line's `totpmt` on 229,658 of 242,697 lines
(94.6%); the fee agrees on 246,509 of 247,212 shared lines (99.7%). The
rest are timing: Services is extracted through September 2026, Payments
through July.

So Monthly Payments does not duplicate Curve Monthly Services and neither
supersedes the other. Services is the charge, Payments is what happened to
it. Reconciliation takes charges and fees from Services and cash, by
transaction, from Payments, and never sums `fee` across Payments rows.

One spec consequence. The Services spec types `totpmt`, `totadj` and `totref`
as text; the values are money in the vendor's own formatting (`$999.99`,
parenthesised negatives, 70 of them). They should be currency. That is a
typing change on a loaded source, which is the D18 case the decision record
describes, so it waits for D18; the comparison above cast them in SQL.

## 18. WeInfuse administrations line up with Curve service lines on patient number, date and HCPCS

Asked 2026-10-23 by the developer: can Curve Monthly Services be used for
accounting reconciliation against the WeInfuse dispenses. Measured on the
current rows in `sbh_data_bank`, counts only.

**The patient identifier is shared.** WeInfuse's `patient_identifier` is
Curve's `patient_number`: 1,908 of 1,940 distinct WeInfuse patients (98.4%)
appear under that number in Services. It is not Curve's `mrn` (3 of 1,940).
Name plus date of birth matches only 1,081 of 2,801, so the number is the
join, not the demographics.

**The drug code is shared.** 70 of the 79 HCPCS codes WeInfuse bills appear
as `proccode` in Services, whose J and Q codes are 39,382 lines.

**Line level, January 2025 to July 2026** (the window both cover; Curve
runs through September, WeInfuse through September, Payments through July):

| | count |
|---|---|
| WeInfuse administration rows | 29,291 |
| with a Services line on the same patient and date, any code | 29,066 (99.2%) |
| with a Services line on the same patient, date and HCPCS | 23,594 (80.5%) |
| Services J/Q lines (not voided) | 15,932 |
| with a WeInfuse row on the same patient, date and HCPCS | 10,248 (64.3%) |

**Grouped by patient, day and HCPCS** (the unit a claim line is billed at),
Buy & Bill inventory only, with WeInfuse's `billing_units` summed per group
against Services' `units` summed per group (voids netted):

| | count |
|---|---|
| administered groups | 11,909 |
| billed J/Q groups | 14,250 |
| matched on patient, day and HCPCS | 7,247 |
| of those, units equal | 7,036 (97.1%) |
| administered, not billed under that code that day | 4,662 |
| billed, not administered under that code that day | 7,003 |
| administered and billed under that code one day either side | 1 |

Specialty Pharmacy inventory (drugs the pharmacy ships for the patient)
is billed too: 1,486 of its 1,594 groups have a J/Q line.

What this says. The join exists and is clean: where an administration
and a claim line meet on patient, day and code, the units agree 97% of
the time. The gaps are not dates (one adjacent-day match in 4,662) and
not patients (99% of administrations have a Curve line that day); they
are codes. WeInfuse's HCPCS and Curve's `proccode` disagree for the same
administration often enough that 4,662 administered groups and 7,003
billed groups find no partner, and the billed side also carries drugs
WeInfuse never sees (office stock, vaccines, infusions recorded
elsewhere). So reconciliation is possible and worth doing, and its
first rule is a code crosswalk: which `proccode` Curve bills for which
WeInfuse HCPCS, learned from the pairs that do match on patient and day
with different codes. That is a curated-layer rule for the reconciliation
phase, after the ETL is signed off, not an ingestion change.

## 19. The database against the raw files: complete, and what the duplicates are

Run 2026-10-23 with `scripts/verify_load.py`, at the developer's request,
before any automation is deployed. Three checks, all read-only.

**Every file is accounted for.** Every data file under the root (zip
members included) was hashed and looked up: 208 loaded, 14 byte-identical
copies or sheets a spec recognises and deliberately hands to another
source, 9 unclaimed by any spec (the six "Reports for Download" index
workbooks, the two zip copies of the backlog, the spec-mapping workbook),
0 not in the database. **Every loaded file's lines are in `landed`**: an
independent count with the plain CSV and workbook readers matched
`rows_landed` on all 208, so no load dropped or invented a line. **No
source has an identity with two current rows, every source's deliveries
were committed in as-of order, quarantine is empty and no batch holds.**

**What deduplication did**, current rows against rows landed:

| Source | Landed | Current | Superseded | Why the difference |
|---|---|---|---|---|
| ehr.curve.monthly_services | 391,909 | 274,476 | 108,890 | overlapping monthly windows; corrections from columns that come and go |
| tpa.pharmaforce.claims | 253,616 | 217,231 | 763 | overlapping windows, unchanged rows |
| ehr.allscripts.erx | 250,966 | 167,175 | 0 | overlapping windows, unchanged rows |
| ehr.athena.eligibility | 94,440 | 24,195 | 946 | snapshot re-pulled each delivery |
| dispense.weinfuse.administered | 86,814 | 62,831 | 482 | overlapping windows; D14 stamp excluded |
| finance.monthly_payments | 1,287,742 | 1,287,742 | 0 | forty monthly files, no overlap |

**In-file duplicates: what the vendor sent twice under one key.** The
rule keeps every row as an occurrence (CLAUDE.md, "Duplicates"), so
nothing was lost; the question is whether the key is right. The columns
that differ between the rows were compared in memory (names only):

- **finance.monthly_payments, 181,422 keys.** Every one is an `I`/`O`
  transfer pair: the same voucher, service, sequence and void sign, one
  row transferring responsibility out and one in, differing in
  `transtype` and the remittance columns. Adding `transtype` to the key
  makes it unique across all 1,287,742 rows (checked). The key should
  change; it is a curated-layer rule on a loaded source, so it waits for
  D18 and is the first item in that queue.
- **wholesaler.cardinal.invoice_summary, 424 keys.** The 2026 pulls are
  one row per line while the key is per invoice (D16), so the lines of
  one invoice differ only in `purchase_amount`. Correct as occurrences;
  a line number from Cardinal would make it a key.
- **tpa.wellpartner.invoices, 10 keys**, differing only in
  `entity_name`: the same wholesaler invoice attributed to two entities.
  A question for Wellpartner, or `entity_name` joins the key.
- **wholesaler.abc.purchases, 3 keys**, differing in the ABC item
  number, description and cost: two items with one NDC on one invoice.
  The ABC item number would complete the key.
- **wholesaler.mckesson.invoice, 9 keys; wholesaler.mckesson.custom, 3
  keys**: the split shipments of finding 16, differing in quantities and
  the original invoice number. Both lines are real.
- **ehr.allscripts.erx 8,970, ehr.curve.appointments 418,
  ehr.athena.prescriptions 47 keys**: byte-identical rows (the vendor
  sent the record twice); kept as occurrences, flagged as findings.

**Typing.** The replay of every source from the landed layer under the
current specs changes nothing (0 of 3,044,698 lines). One typing defect
is known and waits for D18: Curve Monthly Services' `totpmt`, `totadj`
and `totref` are text and should be currency (finding 17).

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

4. **Is `Monthly Payments` the right source for financial reconciliation?** Answered
   2026-10-23, section 17: it is the posting ledger behind `Curve_Monthly Services`,
   and both are needed.
