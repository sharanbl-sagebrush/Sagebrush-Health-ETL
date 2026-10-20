# Review: reference.wholesaler_accounts

Reviewed 2026-10-15 against `docs/layer-walk/reference.wholesaler_accounts.md` (generated the same day) and
`specs/reference.wholesaler_accounts.yaml`. Counts and column names only; no cell values.

## Verdict

Safe to load as-is for a first load, not safe for re-deliveries. The workbook lands, types and dedups cleanly
(6 sheets bind, 247 rows, 0 quarantined, natural key unique in every sheet, late replay moves nothing), and the
counts behind the spec's claims are confirmed with one correction (McKesson 62766 spans four `Class` values, not
three; three is the `Class_New` count). The blocking problem is ordering: the walk takes as-of from the xlsx
`dcterms:created` stamp (2026-01-16), which on a hand-edited register never moves, so the next edited copy finance
sends will carry the same as-of with a different hash and every changed row becomes a `conflict`. Order this file by
`dcterms:modified` (2026-05-05 on the present copy) or an operator-supplied as-of, and treat each delivery as a full
snapshot. The presentation view also needs a decision on the 5 account keys (14 rows) that join to more than one row.

## Deliveries and as-of

- One physical copy under the root: `Original Files/Master COGS List.xlsx`, 35,546 bytes. No other copy in any
  folder, including the two skipped folders. Nothing to compare; nothing newer.
- Stamps: `dcterms:created` 2026-01-16, `dcterms:modified` 2026-05-05, filesystem mtime 2026-09-04 (OneDrive sync,
  matches the decision date in the spec). `docProps/app.xml` says Microsoft Excel Online, not a vendor portal.
- The CLAUDE.md as-of rule was verified for portal exports, where "created" is the extraction time. This file is
  created once and edited for months; "created" is the birth of the workbook, not the state of the register. Under
  `key_occurrence_asof.v1` a re-delivery after an edit has the same as-of and a different hash on every edited row
  (conflict, batch held), and a row finance deletes can never be `vanished`. The modified stamp is bumped on every
  save and is monotonic for a single hand-maintained workbook, so it is the right as-of here. It is not perfect (a
  save with no change bumps it; an unsaved co-authoring session may lag), so an operator override should remain.
- The spec model has no as-of configuration today (no `as_of` field in `registry/spec.py`), and `layer_walk.py`
  `xlsx_created` reads only `dcterms:created`. A per-spec `as_of_source: modified_stamp` is the smallest change.
- All six sheets are six deliveries sharing one as-of; identity includes `source_sheet`, so they never collide.

## Landed

- Sheets in workbook order: Master Sheet, Cardinal, R&S, Mckesson, Cura, ABC. All visible, all six bind, none left
  over. Master Sheet -> `master_sheet`; the five tabs -> `vendor_tab`. Exactly one variant matches each sheet
  (`Class_New` is the discriminator, present only on Master Sheet).
- Headers: Master Sheet 7 named cells plus 4 empty trailing header cells (columns H:K, trimmed by the reader; 0
  stray non-empty cells beneath them). Tabs 5 named cells each. R&S spells the account header `Account no`; both
  spellings are in `sources` and header folding makes them one anyway. No extra, missing or prohibited columns.
- Data lines: 121 / 67 / 3 / 32 / 10 / 14 = 247. Cardinal has 4 trailing blank rows after its data, held back by the
  reader (no `BLANK_ROW` quarantine); no interior blank rows anywhere.

## Typed

- 0 rows quarantined in any sheet; expected, and no spec change is needed to change that.
- `Account No` is an integer cell in all 247 rows; `str()` of an int gives clean digits. 0 of 121 Master accounts
  carry a leading zero (lengths 5 to 10), so the leading-zero concern in the spec is about the purchase feeds' side.
- `Class_New` empty on 1 row; the column is optional so this is a typed null, not a quarantine. Correct.
- `location`: `parse_text` trims, so the typed value loses the leading spaces on 36 Master rows and the trailing
  space on 1, while the 115 embedded newlines survive. The spec's description ("leading spaces ... preserved") is
  true of landed only. No change recommended, but the description should say so.
- Null-placeholder risk (`NULLISH` includes "na", "-"): 0 cells in any key or class column fold to a placeholder.

## Curated

- Natural key `(source_sheet, payee, account_number, location)` unique in every sheet, raw and after whitespace/
  case folding: 121 / 67 / 3 / 32 / 10 / 14 distinct, 0 duplicate groups, max occurrence 1.
- Master Sheet: `(payee, account_number)` has 112 distinct values, 5 groups of >1 row covering 14 rows (sizes 2, 2,
  2, 3, 5). `account_number` alone gives the same 112 / 5 / 14: no account is shared across payees.
- McKesson 62766: 5 rows, 5 distinct locations, 4 distinct `Class`, 3 distinct `Class_New`, 1 category, 1 of the 5
  flagged changed. CLAUDE.md and data-findings say "three different classes"; that is the `Class_New` count.
- Reclassification: `Class` != `Class_New` on 28 of 121 rows (confirmed), 7 distinct old->new pairs. `Changed?` has
  a two-word vocabulary (93 / 28) and agrees with the computed difference on 121 of 121 rows. One of the 28 has an
  empty `Class_New`, so it is 27 reclassifications and 1 declassification.
- Consolidated vs tabs: tabs hold 126 rows, Master 121. Each tab's single payee maps to exactly one of Master's 5
  payees (no overlap, no unmatched). Cardinal 67 = 67, R&S 3 = 3, Cura 10 = 10, all keys and classes agree.
  Mckesson 32 vs 28: 4 tab rows have an (account, location) absent from Master; each is an account with 1 Master row
  and 2 tab rows (a second ship-to the Master does not carry). ABC 14 vs 13: 1 tab row whose account sits in Master
  under a different payee. Keys only in Master: 0. So the "five rows in a tab but not in Master" claim holds.
- On the 121 shared keys, `Class` disagrees on 5 (McKesson 4, ABC 1), `Category` on 0. Tab `Class` differs from
  Master `Class_New` on 29, so the tabs track the old classification, not the proposed one.
- No key matches only after whitespace or case folding between any tab and Master (0 of 126).
- Late replay: 0 current rows changed, as reported. Note the test is weak for this source: all six deliveries share
  one as-of, so no row can be `late`; the replay simply reports 14 unchanged.

## Presented

- The purchase feeds carry `account_number` (all four) and `account_name` (all four); McKesson invoice also
  `facility`, `account_classification`, `associated_340b_id`, `street_address_history`; Cardinal `account_alias`;
  ABC `city`, `state`. None carries the register's multi-line `location` (115 of 121 are multi-line, one is 13
  lines), so `location` cannot be a join column.
- Proposed view: Master Sheet rows only (`source_sheet` = Master Sheet), one row per `(payee, account_number)`,
  with payee folded to the vendor key the purchase specs use, exposing `category`, `class_current`,
  `class_proposed`, `class_changed_flag`, and a derived `site_status` = RESOLVED / UNRESOLVED (the 28) / AMBIGUOUS.
- Join on `(vendor, account_number)`: 112 keys, 107 join to exactly one row, 5 keys join to more than one (14
  rows). Of those 5, 3 collapse to a single `Class` and `Class_New` and all 5 to a single `Category`, so with a
  collapse rule only 2 keys (7 rows, one of them 62766) are truly ambiguous on site. The first line of `location`
  separates the rows in just 1 of the 5 groups, so a purchase-side ship-to field (McKesson `facility` or
  `street_address_history`) is the only candidate disambiguator and needs a probe against real purchase rows.
- 78 of 121 `Class` values (74 of `Class_New`) name a contract pharmacy or "Unassigned", not a clinic: for the
  prescription-340B channel the view resolves to a channel partner, not a covered-entity site. The view should say
  which, via `category`.

## Proposed event-date column

None. The register has no date column at all (`Class_New` has no effective date, open question 1). It is a
dimension snapshot: declare the spec as a full-replacement source so a newer delivery marks every current key it
omits as `vanished`, with the delivery as-of (modified stamp) acting as `valid_from` of each row version. The
`vanished` outcome, not an event date, is what this source needs from the engine. Separately, ask finance to add an
`Effective Date` column beside `Class_New`; if they do, map it as `class_effective_date` (type date, optional).

## Open questions for the developer

1. Order this source by `dcterms:modified` instead of `dcterms:created` (spec-level `as_of_source`)? Yes/no.
2. Keep an operator-supplied as-of override for hand-maintained references even when a stamp exists? Yes/no.
3. Declare this spec a full-replacement snapshot so omitted keys become `vanished` on the next delivery? Yes/no.
4. Should `layer_walk.py` report `dcterms:modified` and the `Application` tag beside the created stamp, so a
   hand-edited workbook is visible as such? Yes/no.
5. Presentation join key: `(vendor, account_number)` with a collapse rule for the 3 single-class groups and
   AMBIGUOUS for the 2 remaining keys, or hold all 5 keys as AMBIGUOUS until a ship-to disambiguator is proved?
6. Correct CLAUDE.md and data-findings to "four `Class` values, three `Class_New` values" for 62766? Yes/no.
7. Treat the 1 row with empty `Class_New` as declassified (UNRESOLVED) or as unchanged? Named decision.
8. Report the 5 shared-key `Class` disagreements and the 5 tab-only keys as one exception list in pres? Yes/no.
9. Amend the `location` description to say leading/trailing spaces are preserved in landed, trimmed in typed? Yes/no.

## Probes run

- probe1: read `docProps/core.xml` and `app.xml` for created/modified/Application; listed sheet names, states,
  dimensions and raw header cells per sheet.
- probe2: per sheet, counted blank/trailing rows, cells beyond the header width, cell types, leading/trailing
  whitespace, embedded newlines, double spaces, null placeholders; natural-key and sub-key uniqueness raw and
  folded; 62766 row/class counts; `Class` vs `Class_New` differences and `Changed?` agreement; tab-vs-Master key
  and class/category agreement; join multiplicity on `(payee, account)` and `account`.
- probe3: for tab-only keys, counted Master rows and tab rows per account and whether any match survives
  whitespace or alphanumeric folding; distinct `Class` per `Category`; location first-line and line-count
  distributions; the blank `Class_New` row's flag.
- probe4: re-counted 62766 classes with alphanumeric folding; `find` over the root for other copies of the workbook.
