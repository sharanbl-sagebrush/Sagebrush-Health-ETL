# Review: finance.monthly_payments

Reviewed 2026-10-16 against `docs/layer-walk/finance.monthly_payments.md` (generated 2026-10-15) and
`specs/finance.monthly_payments.yaml`. 40 CSV members in one zip, one per calendar month from 2023-04 to
2026-07, 1,287,742 rows, 116 columns. Members are referred to by month; M40 = `2026-07 upto 07212026`.
Sibling review: `ehr.curve.monthly_services.analysis.md` (98.8% of Curve service ids appear here at transaction grain).

## Verdict

Loads cleanly but the curated and presented layers are wrong by construction. Every member binds to the one
variant, 0 rows quarantine, every parenthesised amount types negative, SSN is matched and dropped in all 40
members, the late-file replay changes nothing, and no member ever re-sends an earlier month. But the 14,956
rows the walk labels "corrected" are not corrections: each is a void reversal posted 1 to 27 months later that
reuses the original line's full four-column key (including `SeqNum`), so the dedup rule supersedes the
original with its reversal and the pair that should net to zero is reduced to the reversal alone. The presented
`amount` sum is 189,176,073.27 against 191,240,346.67 over the union: understated by 2,064,273.40 and short by
14,956 rows. Adding `voidsign` to the natural key separates every one of those pairs (all 14,956 go VoidSign 1
to -1) and removes the defect; the spec's claim that the key is unique over 1,287,742 rows does not hold
(181,688 within-file groups, 14,956 cross-month). Declare `transdate` as the event date.

## Deliveries and as-of

- As-of from the filename is right as an ordering key: `TransDate` falls inside the filename month on 100% of
  rows in all 40 members (M40: 2026-07-01 to 07-20), 0 rows after the as-of in any member, and the 40 windows
  partition the data with no overlap. M40 overlaps no full month.
- It is not the extraction time. In all 14,956 cross-month pairs the earlier row is already `IsVoided` true
  for a reversal dated months later (gap in deliveries: 1 month 2,844; 2 to 6 months 8,181; 7 to 12 months
  2,974; 13 to 27 months 957). `Date_Paid` exceeds the month end in 3 members (2024-07: 201, 2024-08: 56,
  2024-12: 38 rows). The months were extracted (or re-extracted) long after they closed, most likely in one
  pass. CLAUDE.md's floor (as-of not earlier than the latest event date) must be computed on `transdate` only.
- A month file is a closed period in the sense that matters: 0 identities appear in more than two members and
  every cross-member recurrence is a new reversal event, not a re-send. Whether a later drop could re-send an
  earlier month with changed void flags is unobserved; nothing in 40 members does it.
- `TransMonth` is the accounting period, not the month of `TransDate`: equal to the file month on 952,573 rows,
  one month earlier on 243,868, and the entire 2023-04 to 2023-08 members plus 5,972 rows of 2023-09 (93,581
  rows) carry one period, Mar 2023, which stayed open through September (go-live). `PostMonth` is the period of
  the underlying charge: earlier than `TransMonth` on 843,391 rows, later on 2,794, blank on 37, and after the
  as-of on up to 434 rows per member (2025-03). The spec comment "PostMonth can be months after TransMonth" has
  the direction backwards for 99.7% of the rows where they differ.
- Event date: `TransDate` (see Proposed event-date column). `SvcDate` is in the file month on only 2% to 20% of
  rows per member; `Date_Paid` reaches back to 1966, 1982, 1985, 2000 in several members and is not a cut.
- Vocabulary for the engine: event date = `transdate`; coverage = filename month window on `transdate`;
  as-of source = filename; vanish scope = not needed while no member re-sends a month (see Open questions).

## Landed

- Header: 116 columns, identical in name and order across all 40 members (0 added, 0 removed). `SSN` is at
  position 57 in every member, is matched by the spec, dropped as prohibited in all 40 (walk: "ssn" in every
  row of the landed table), and was excluded from `usecols` in every probe; no probe output contains it.
- Encoding: UTF-8 BOM on every member; CRLF line endings on every member. Nine members carry bare LFs inside
  quoted cells (2023-06: 6, 2023-10: 7, 2023-11: 8, 2024-04: 6, 2024-08: 6, 2024-12: 20, 2025-01: 8,
  2025-11: 11, 2025-12: 3; 75 in all); the csv parser handles them and the row counts are unaffected.
- Physical lines = rows + 2 (header plus one trailing blank line) in the 31 clean members, and rows + 2 plus
  the embedded LFs in the nine above. Landed "data lines" equals parsed rows in all 40; the walk's 1,287,742
  reconciles to the bytes. No footer in any member.
- No extra, missing-optional or unmatched columns anywhere.

## Typed

- 0 quarantined cells in any member; no spec change is needed for parsing. All five date columns parse on
  every non-blank row (0 unparseable in any member); the only blanks are `PostMonth` (2, 2, 5, 28 rows in
  2023-07, 2023-11, 2026-06, M40).
- Parenthesised amounts: 227,284 across the 40 members (per member 32 to 13,749), the spec's figure. In every
  member the parenthesised count equals the negative-after-typing count, 0 parse failures, 0 null amounts.
  Sum of `Amount` over all rows: 191,240,346.67, the spec's correct signed total.
- `Amount` is already signed. `VoidSign` is a flag, not a multiplier: multiplying gives 195,465,142.93, which
  double-negates the 15,486 reversal lines. Nothing downstream should multiply by it.
- `amount` as control_total: yes. There is no footer to check against, but the batch sum is the figure that
  detects the 2,064,273.40 curated loss, and `fee` already has the same treatment proposed in the spec notes.
- Date plausibility (not a parse failure): `Date_Paid` minimum year is 1966, 1982, 1985, 2000, 2004, 2005,
  2006, 2016 in eight members. A spec-level minimum-date rule would be a new feature, not a fix here.

## Curated

- Natural key uniqueness: not unique within a month (181,688 groups of two to four rows, in every member,
  296 to 10,971 per member) and not unique across months (14,956 identities in exactly two members each).
  The findings document (section 13) says unique over 1,287,742 rows; that is wrong.
- Within-file groups are paired legs of one transaction, not duplicates: adjacent in the file in 181,688 of
  181,688, same `TransCode` in all, different `TransType` in 181,334, same `VoidSign` in 181,246, and `Amount`
  sums to zero in all 181,688 (19,266 groups are zero-amount on both legs). Occurrence numbering keeps both
  legs; this is correct and they are never re-sent.
- Cross-month "corrections" are void reversals. Columns differing, with pair counts: `VoidSign`, `VoidFlag`,
  `TransDate`, `TransMonth`, `BatchNum` 14,956 each; `OperatorName` 14,124; `BatchCategory` 11,216; `WorkRVU`
  9,716; `Amount` 9,128; `DxCode4`/`DxDesc4` 4,198; `DxCode3`/`DxDesc3` 3,596; `DxCode2`/`DxDesc2` 2,169.
  Never differing: `PostMonth`, `IsVoided` (true on both), `TransType`, `TransCode`, the key, patient columns.
  Earlier row VoidSign 1 and later -1 in 14,956 of 14,956; `Amount` negated in 9,128, equal and zero in 5,828.
- Void accounting: 15,486 reversal lines, 15,486 flagged originals; 14,956 pairs span months and 530 fall in
  one month (442 of the within-file groups have differing VoidSign). The sum of the superseded originals is
  2,064,273.40 (all of it from the 9,128 negated pairs).
- With `voidsign` added to the key every cross-month pair becomes two identities (no pair shares VoidSign),
  corrections drop to 0, current rows become 1,287,742 and the current sum equals the union sum. The within-file
  pairs are unaffected (same VoidSign in 181,246; the 442 others become occurrence 1 of two keys).
- Late replay of 2023-04 after everything: 13 late, 2,267 unchanged, 0 current rows changed. Correct, and the
  13 are exactly that member's rows later superseded by reversals.

## Presented

- Current rows 1,272,786 vs union 1,287,742: 14,956 fewer, every one a voided original replaced by its reversal.
- `amount` over current rows 189,176,073.27 vs 191,240,346.67 over the union (void pairs and transfer legs net to
  zero inside the union, so the union sum is the ledger total). Understated by 2,064,273.40.
- Current rows by member equal rows minus superseded originals in every member (e.g. 2024-12: 68,206 less 287).

## Proposed event-date column

`transdate`. Required, never blank, 0 parse failures, inside the filename month on 100% of rows of all 40
members, never after the as-of, and the 40 windows partition the data with no overlap. `transmonth` is an
accounting period that stayed open for six months at go-live and trails `transdate` on 243,868 rows;
`postmonth` is the charge period, before `transdate` on most rows and after the as-of on some; `date_paid`
exceeds the month end in three members and carries decades-old values; `svcdate` precedes the window on most
rows. Coverage = filename month on `transdate` (M40: 2026-07-01 to 2026-07-21). Note the as-of floor and
coverage must be computed on `transdate` alone or `date_paid` and `postmonth` would refuse or mis-cover files.

## Open questions for the developer

1. Add `voidsign` to `natural_key` ([clientid, voucher_number, service_id, seqnum, voidsign]), which removes all
   14,956 false corrections and restores 1,287,742 current rows and the 191,240,346.67 total: yes/no?
2. Declare `transdate` as the event-date column for coverage and the as-of floor: yes/no?
3. `as_of_source: filename` for this source, accepting that the real extraction time is months later than the
   as-of (the files already know about later voids): yes/no?
4. Vanish: disabled for this source, since no member re-sends an earlier month and a re-extracted month would
   carry new void flags as corrections rather than removals; or enabled with coverage = filename month on
   `transdate`: named decision.
5. Period assignment: the spec says `postmonth`, which precedes `transmonth` on 843,391 rows (it is the period of
   the charge, not of the payment). Which period does finance reconcile a payment to, `transmonth` or
   `postmonth`: named decision.
6. Mark `amount` as `control_total: true` (batch sum recorded even without a footer): yes/no?
7. Confirm the 181,688 within-file paired legs (same key, same TransCode, different TransType, net zero) are
   both to be presented, as occurrence 1 and 2: yes/no?
8. Treat `voidsign` as a flag only and never multiply `amount` by it in any view: yes/no?
9. Correct `docs/data-findings.md` section 13 (key not unique: 181,688 within-file groups, 14,956 cross-month)
   and the spec's natural_key comment that rests on it: yes/no?
10. Should `date_paid` values before `coverage_start` (eight members, back to 1966) quarantine under a
    plausibility rule, or stay typed as delivered: named decision.

## Probes run

- p1 (`finance.monthly_payments_p1_landed_keys.py`): per member from raw bytes: BOM, CRLF/LF counts, physical lines, header drift against member 1, parsed row count; parenthesised vs negative vs failed amounts; blank, unparseable, in-filename-month and after-as-of counts for TransDate, TransMonth, PostMonth, Date_Paid, SvcDate; PostMonth vs TransMonth ordering; within-file key groups with VoidSign, amount-cancel and voided counts; in-memory dedup by (key hash, occurrence) in as-of order with naive and current amount sums and current rows by member.
- p2 (`finance.monthly_payments_p2_corrections.py`): all 40 members, identities by (key hash, occurrence); recurrence count, member gap, TransMonth and PostMonth offsets from the file month; key+TransDate and key+TransDate+Amount duplicate groups; within-file TransType/TransCode/adjacency; full-row diff (all columns except SSN) of every cross-member pair: differing columns, amount relation, TransDate gap, PostMonth and IsVoided relation, occurrence number.
- p3 (`finance.monthly_payments_p3_voidsign.py`): VoidSign and (IsVoided, VoidSign) distributions; Amount sum vs Amount x VoidSign sum; cross-member pairs by (VoidSign earlier, VoidSign later, amount relation, zero amount) with the sum of superseded amounts; within-file groups by VoidSign and TransType agreement and signed cancellation.
