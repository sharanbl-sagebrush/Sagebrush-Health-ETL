# Layer walk: what every source looks like through the four layers

Generated 2026-10-15 from `scripts/layer_walk.py` (one `<key>.md` report per
source, counts only) and one reviewer note per source (`<key>.analysis.md`,
with a verdict, findings per layer, a proposed event-date column and numbered
yes/no questions). This file consolidates the nineteen notes into the
decisions they ask for. Nothing here changes code or specs; it is the list to
work through before any source is loaded.

All nineteen notes are in.

## Scorecard

| Source | Verdict | Blocks loading? | Proposed event date |
|---|---|---|---|
| wholesaler.mckesson.invoice | safe as-is | no | invoice_credit_date |
| wholesaler.mckesson.custom | safe as-is; subset of invoice report from 2026-03 | no | invoice_credit_date |
| wholesaler.cardinal.invoice | safe as-is; summary workbook holds 2023 to 2024-04 purchases the detail lacks | no | invoice_date |
| wholesaler.abc.purchases | safe with changes (as-of from filename, hash excludes price refs, key adds `abc`) | no | invoice_date |
| tpa.wellpartner.invoices | safe as-is; credits positive, filename coverage unreliable | no | wholesaler_invoice_date |
| tpa.wellpartner.claims | safe as-is; reversed claims leave the extract, so vanish is mandatory; D1 CSV is lossy (gpi in scientific notation) | no | dispensed_date (coverage by processed_date) |
| tpa.pharmaforce.claims | safe as-is; vanish must stay off | no | fill_date |
| tpa.prxp.transactions | loadable, not presentable: header renames, email PII, money as text | spec fixes first | rx_fill_date |
| dispense.weinfuse.administered | loadable, not interpretable: text timestamps, superset of buybill | spec fixes first | administration_date |
| dispense.weinfuse.buybill | safe as-is; subset of administered; date typed as text | no | date |
| dispense.weinfuse.patients | loadable, wrong across variants: Created Date misbound, hash per variant | spec fixes first | last_updated (legacy only); vanish off |
| ehr.athena.encounters | safe as-is; as-of floor needs 1-day tolerance | no | cln_enc_date |
| ehr.athena.prescriptions | safe as-is; rows overstate orders 8x | no | order_chartdate |
| ehr.athena.eligibility | safe for the three "as of" snapshots; Feb file is a different report | no | the delivery as-of (snapshot) |
| ehr.allscripts.erx | loadable; June file as-of must be supplied | operator input | ordered_date |
| ehr.curve.appointments | NOT loadable: 3 of 4 files quarantine 100% on timestamp format | yes | appointment_datetime |
| ehr.curve.monthly_services | key `(service_id, voidsign)` adopted 2026-10-16; loads; remaining items are typing fixes | no | postdate |
| finance.monthly_payments | `voidsign` added to the key 2026-10-16; loads cleanly | no | transdate |
| reference.wholesaler_accounts | safe for first load; order by modified stamp, full-replacement snapshot | no | none (snapshot) |

Across all nineteen walks: zero conflicts, every late-file replay left current
rows unchanged, every byte-identical duplicate file was refused, and no
reviewer found a fault in the walk tool. The dedup rule holds. What the
reviews found is upstream of it: spec typing, identity choice, delivery scope.

## Decisions the engine needs (cross-cutting)

Each item names the sources that raised it and the recommendation. The
per-source questions are in the notes; these are the ones that must be
answered once for everyone.

**Status 2026-10-16:** answered. The developer's answers are the `ANS:` lines
below; the resulting decisions and defaults are in `docs/decision-record.md`,
which is the file to read. This section is kept as the record of the questions.

### D1. Declare the event date in the spec

Every reviewer proposed a column (scorecard). The spec model has no field for
it, and the vanish rule cannot run without one. Recommendation: add
`event_date: <target>` to SourceSpec, with the value `as_of` allowed for
snapshot sources (eligibility, wholesaler accounts, patients) where a newer
delivery covers every row of an older one.
ANS: Go ahead and implement your solution

### D2. Define delivery coverage per source, not by one rule
ANS: For each source every Sunday, we will download the last 3 weeks of data to enrure we capture the data that was added later on. So, every new file will have 2 weeks of overlapping data.

The filename window is right for most sources, wrong for several:

- Cardinal excludes the extraction day: coverage ends at as-of minus one day.
- WeInfuse includes the whole day before the filename start.
- Wellpartner Invoices D3 claims January but holds only March: coverage must
  come from the data range, or vanish stays off.
- Allscripts carries a few orders before the filename start: window stays the
  filename range, pre-start rows insert and are never retired by that file.
- PRXP: coverage end = max(filename end, latest event date).
- Wellpartner Claims: the portal filters on processed_date while the event is
  dispensed_date, which precedes the window on up to 494 rows per file. The
  spec needs a coverage column distinct from the event date, or vanish stays
  off. Both Wellpartner reports show the "since last extract" pattern.

Recommendation: `coverage: filename | data | as_of` plus optional
`coverage_start_offset_days` and `coverage_end_offset_days` on the spec.

### D3. Scope vanish by more than date
ANS: I didn't get this section.

- McKesson (both reports): scope by account_number; the June extract carries
  two accounts absent from earlier extracts for the same dates.
- Pharmaforce: scope by opaid, or a Southington file retires every Fire Mesa row.
- WeInfuse Administered: scope by location_id.
- Wellpartner Claims: scope by n340b_id. Reversed claims simply leave the
  extract (22 D1 rows stay current today), so vanish is mandatory there.

Recommendation: `vanish_scope: [column, ...]` on the spec, and vanish disabled
(`vanish: false`) for patients, pharmaforce (until its report-scope question
is answered) and any source without a declared event date.

### D4. The as-of floor needs a tolerance and a column
ANS: I didn't get this section.

Athena Encounters and Curve Appointments carry scheduled rows dated the day
after the pull; Curve Monthly Services has a future-dated column reaching
2027. Recommendation: compute the floor over the declared event date only,
and allow a one-day tolerance.

### D5. Where as-of comes from, per source
ANS: What if we create a new folder named as today's date and inside we keep all the downloaded folders. So, the folder name can be considered as as-of date to use same rule for all the source files

- ABC: the xlsx stamp is an Excel Online re-save by a person; use the filename.
- Master COGS List: a hand-edited register whose created stamp never moves;
  use the modified stamp or an operator date.
- Allscripts June file: data runs three days past the filename; the operator
  must supply 2026-06-10 or the file is refused.
- Several vendor files were re-saved in Excel before upload (Cardinal D2,
  WeInfuse 06-26, PRXP D1, Wellpartner D2). The stamps still match the
  filenames, but the stored hash is not the vendor's bytes.

Recommendation: `as_of_source: xlsx_created | xlsx_modified | filename |
operator` on the spec, default `xlsx_created` then `filename`; the operator
override exists for every source.

### D6. What the row hash covers
ANS: I didn't get this section.

- Patients: the hash runs over each variant's own columns, so every row the
  new module touches looks corrected and loses its site key. Hash over the
  union of all variants' targets, nulls included.
- ABC, McKesson Custom, PRXP, Curve Appointments: reference-price and
  balance columns refreshed at extraction time produce corrections that are
  not vendor edits. Exclude them with the existing `row_hash_fields`
  mechanism, or add `row_hash_exclude` to the spec.

Recommendation: union-of-variants hashing always; per-spec exclusion list for
snapshot columns, named in the spec so the choice is reviewable.

### D7. A datetime type
ANS: Implement recommended

WeInfuse vendor_created_at, Curve Appointments timestamps, PRXP modified stamps and
patients' last_updated are either typed text (so a format change becomes
17,000 fake corrections) or typed date (so the time is silently dropped).
Native xlsx datetime cells also bypass `date_formats` entirely.
Recommendation: add `datetime` to FieldType with offset-aware formats, and
make the xlsx reader check native cells against the declared type.

### D8. Keys the data has that the specs say it does not
ANS: I didn't get this section.

- Curve Monthly Services: (service_id, voidsign) unique in all four files.
  Adopt it. This is the $85.3M finding.
- Athena Encounters: five columns unique in every file; one is a status.
  Named decision.
- Athena Prescriptions: (cln_enc_id, order_id, icd10) unique except one day.
  Named decision.
- Allscripts: the 14 non-PII columns give the same duplicate counts as all
  21; using them stops demographic edits counting as churn. Named decision.
- ABC: add `abc` to the key, unique in every file.
- Monthly Payments: the four-column key is not unique (181,688 paired legs
  within files, correctly kept as occurrences) and void reversals posted
  months later reuse it in full, so the rule supersedes originals with their
  reversals. Add `voidsign` to the key, mirroring Curve Monthly Services.

### D9. Report of record where two sources carry the same rows
ANS: Keep all the source files into it's separate tables. For example, 2 McKesson source must have it's own tables. Same for Curve monthly services and payments

- McKesson: Purchasing Detail is the purchase source; Custom is a strict
  subset from 2026-03. Loading both doubles $21.7M.
- WeInfuse: Administered is a strict superset of BuyBill (45,013 of 45,013
  keys). Retire BuyBill or present it as a flag on Administered.
- Cardinal: the summary workbook, currently recognise-only, holds 5,326
  invoices ($16.2M) from before the detail report's 24-month cap. Reopen.
- Curve Monthly Services vs Monthly Payments: 98.8% of service ids appear in
  payments at transaction grain. Services is charge grain, payments is
  transaction grain; both are needed, and both need `voidsign` in the key.

### D10. Presentation grain
ANS: I didn't get this section.

Athena Prescriptions rows are order times encounter (8.2x overstatement);
count order_id. Wellpartner Invoices credits are positive-signed; net them by
invoice_type. McKesson net_quantity differs from filled plus returned on
credit rows. Each note says which column to count.

## Spec fixes the notes name (no decision needed, just work)

| Source | Fix |
|---|---|
| ehr.curve.appointments | add `%m/%d/%Y %I:%M %p` and `%I:%M:%S %p` to the six timestamp columns; three files quarantine 100% without it |
| tpa.prxp.transactions | alias `Referral Type List`; new target `waiting_for_check_time`; declare `patient_email` with a PII class; PII on DOB and patient ids; currency on eight money columns |
| dispense.weinfuse.buybill | type `date` as date with ISO then US formats |
| dispense.weinfuse.administered | fix the filename pattern lookahead (it matches BuyBill files); split `Created Date` from `Created At` |
| dispense.weinfuse.patients | rebind `Created Date` to a new `created_date` target |
| ehr.curve.monthly_services | currency on totpmt/totadj/totref, boolean on isvoided, integer on voidsign/encounter; notes say five Textbox columns, there are eight |
| ehr.athena.eligibility | the `Feb042026` file is a different report (title lacks "Active"); exclude or add a variant |
| wholesaler.cardinal.invoice | three wrong comments ("covers the same period", "2024 to 2026 in full", NDC count) |
| wholesaler.abc.purchases | notes still describe identity "falling back to the row hash" |
| ehr.athena.encounters / prescriptions / allscripts / curve | notes restate duplicate counts that are now explained (one-day duplication, void pairs, 7,548 groups) |
| finance.monthly_payments | `amount` as control_total; as-of from filename (`as_of_source: filename`); period assignment transmonth vs postmonth is a named decision; spec comment on PostMonth/TransMonth order is inverted; findings doc section 13 uniqueness claim is wrong |
| tpa.wellpartner.claims | decimal on qty_allocated/unreplenished_qty/order_attempts; datetime on last_modified_date_time; shape rule on gpi; PII class on card_holder |

## PII classification gaps

- PRXP: `patient_email` arrives unclassified under `on_new_column: warn`; DOB
  and both patient ids carry no class.
- Patients and Eligibility: MRN-shaped identifiers kept in clear as join keys.
  Confirm that is the intended posture.
- Pharmaforce: `e_prescribe_mrn` is `none`; `comments` free text.
- Provider names are `direct` on Allscripts and `none` on Athena. Align.
- Curve Appointments: certificate, authorization and comment columns unclassified.

## Questions for the business, not the developer

- McKesson 2025 volume is a fraction of 2024 (616 vs 3,580 rows, two empty
  months). Both reports agree, so it is not an extraction fault.
- Pharmaforce June extract carries zero reversal rows; Southington has
  activity after the date the spec says it ended.
- Athena Prescriptions Jan to Sep 2025 has 9 to 51 rows a month vs thousands
  in 2024.
- Wellpartner carries no ABC invoices at all.
- Who re-saved vendor files in Excel before upload, and can that stop.
- The April 2026 "Administered" file is a byte copy of the BuyBill file; the
  real extract was never taken.
- Master COGS List: `Class_New` still has no effective date (open question 1
  in CLAUDE.md), and 62766 spans four `Class` values, not three.

## Suggested order for the source-by-source review and load

Safe sources first, in reconciliation importance, so the engine is exercised
on clean data before the sources that need spec work:

1. wholesaler.mckesson.invoice
2. wholesaler.cardinal.invoice
3. wholesaler.abc.purchases
4. tpa.wellpartner.invoices
5. tpa.pharmaforce.claims (vanish off)
6. ehr.athena.encounters, ehr.athena.prescriptions, ehr.athena.eligibility
7. ehr.allscripts.erx (after the June as-of is supplied)
8. reference.wholesaler_accounts (after the as-of source decision)
9. dispense.weinfuse.administered and patients (after D6, D7 and the rebinds)
10. tpa.prxp.transactions (after its spec fixes)
11. ehr.curve.monthly_services (after D8)
12. ehr.curve.appointments (after the timestamp formats)
13. finance.monthly_payments, tpa.wellpartner.claims (after their notes)
14. wholesaler.mckesson.custom and dispense.weinfuse.buybill last, or not at
    all, pending D9.

## Status 2026-10-20

The walk tool now runs the real delivery facts (`curate/delivery.py`: as-of
per D5, coverage per D2, the as-of floor per D4) and the real dedup rule
with the `vanished` outcome (`curate/rule.py`), so the "not simulated"
paragraph in older reports no longer applies to a source that declares an
event date. The landed column is now "lines after header" and counts the
footer line, as the typed "read" column does not. Reports regenerated so
far: both McKesson sources, after their specs were declared (see
`docs/decision-record.md`, the two 2026-10-20 sections). The other
seventeen reports are from 2026-10-15 and predate the tool change.

## Status 2026-10-22

The backfill has run: fourteen sources are in `sbh_data_bank`, every
delivery in as-of order, zero quarantined, zero conflicts (the table and
the decisions it forced are in `docs/decision-record.md`, "The backfill").
The five sources whose specs changed after the first migration (Curve
appointments, the three WeInfuse specs, PRxP) had their empty tables
dropped and recreated later the same day and are loaded, together with
the Cardinal summary as its own source (D16, reopening the D9 line
above). Two new files of every source arrived in the `10212026` and
`10222026` folders (May to September 2026) and are in the same load.
PRxP's `Waiting for Check DateTime` is text now, and a file that
quarantines past its spec's rate is held (D17); both came out of those
new files. Numbers in `docs/decision-record.md`, "The second load".

Of the spec fixes in the table above, done: the Curve appointments
timestamp formats (317,804 rows typed, where three files had quarantined
in full), every PRxP item, the BuyBill `date` type, the Administered
filename pattern and the `Created Date` split, and the Patients rebind
(as `vendor_created_date`; D13 forbids the ledger name). The Athena
eligibility `Feb042026` file binds to the existing spec and now has an
as-of (a lone date in a filename). Not done, and visible in the load as
corrections that are not corrections: the WellPartner claims and Curve
monthly services typing refinements, and the stale notes on Cardinal,
ABC, Athena and Monthly Payments. Patients still hashes per variant (the
new module carries far fewer columns), so 340 of its 3,405 rows show as
corrected between modules.
