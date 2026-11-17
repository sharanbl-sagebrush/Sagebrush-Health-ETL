# SBH_DATA_BANK state log

The dated record of what was built, loaded and decided, moved out of
`CLAUDE.md` on 2026-11-10 so a session does not carry it into every
request. Each entry's reasoning and numbers are in
`docs/decision-record.md`; this is the chronology. New entries go at
the end, one bullet per day or event, as before.

## The state of things

- 21 source specs in `specs/`, every one verified by loading the real extract
  (Cardinal's summary and detail reports are two sources since 2026-10-22, D16;
  the Apexus pricing workbook since 2026-10-23)
- `store/schema.py` generates the layered DDL from the specs: `landed`,
  `typed` and `curated` tables per source plus `meta` and `stg`
  (`python scripts/migrate.py --dry-run` prints it, 277 statements).
  **Applied to `sbh_data_bank` on Azure 2026-10-21**, re-applied 2026-10-22
  after the fifteen empty tables of five changed specs were dropped by
  hand, and again 2026-10-23 for the pricing source, `meta.blob` and
  `meta.spec_check`: 63 layered tables plus `meta` and `stg`. The 19 `raw.*` tables
  that predated the layered decision were never written to and were
  dropped with their schema 2026-11-06 (`scripts/drop_raw.py`, developer:
  "Drop the raw tables"). Re-running the migration is a no-op; it only
  creates what is missing (since 2026-11-04 that includes a column a spec
  gained, added nullable to the live tables) and never alters a column, so
  a spec change to a table with rows is a decision about those rows first
- 539 tests, no database required to run them
- Decided 2026-10-16: the two `voidsign` keys are applied; design defaults
  D1-D11 are recorded in `docs/decision-record.md` and reviewed one source
  at a time in the layer-walk load order, McKesson Purchasing Detail first
- Done 2026-10-20: D1-D6 and D11 are implemented (spec fields, `curate/`,
  audit columns); both McKesson specs declare their event date, coverage and
  vanish scope and their walks are regenerated with the `vanished` outcome
  running. D7 (`datetime` type) waits for the first source that needs it
- Done 2026-10-20: the commit engine (`commit/`) writes a load through the
  layers in one transaction and persists what `curate.rule` decides; replayed
  in memory over the real McKesson files it reproduces the walk exactly
  (7,124 and 6,415 current rows, 11 corrections each)
- Done 2026-10-21: **first real load**. The five McKesson Purchasing Detail
  files committed to `sbh_data_bank` in as-of order: four LOADED, the
  byte-identical copy recorded as `DUPLICATE_FILE`. The database agrees with
  the replay exactly: 7,124 current rows, 11 corrected, extended price
  23,305,296.76, 15 duplicate findings, 0 quarantined, no identity with two
  current rows
- Done 2026-10-22: **the backfill**. `scripts/backfill.py` committed every
  delivery of fourteen sources in as-of order on the batched insert path
  (`--fast`, every parameter declared from the DDL, proved with 276
  read-back checks and 0 mismatches). Zero quarantined, zero conflicts,
  engine counters identical to the dry runs. Results and the two decisions
  it forced (D14 creation stamps outside the hash, D15 declared scale) are
  in `docs/decision-record.md`
- Done 2026-10-22, later: **every source is in Azure**. The five changed
  specs had their empty tables recreated and loaded, the Cardinal summary
  loaded as its own source (D16), and the two new upload folders (May to
  September 2026) went in with them, for all eighteen sources that had
  them: zero refused, zero quarantined, zero conflicts, the six changed
  sources read back and balanced. Three decisions came out of the new
  files: D16, D17 (a file past its quarantine rate is held) and the text
  width rule below. Numbers in `docs/decision-record.md`, "The second load"
- Done 2026-10-22, evening: **the freshness check and the landed-layer
  replay**. `scripts/freshness.py` reads the metadata tables and reports
  STALE, NEVER_LOADED, OPEN_REFUSAL, RUN_FAILED and RUN_UNFINISHED as
  alerts (exit code 1) and open quarantine as a note; its first run found
  one real alert, Monthly Payments with nothing since 2026-07-21.
  `scripts/retype.py` types every loaded file again from `landed` plus the
  recorded header, without the file, and compares line by line with
  `typed` and `stg.quarantine`: over the whole database every line came
  back with the same hash and every control total agreed, which is the
  proof that the landed layer is complete. It reports only; writing the
  rebuilt rows is D18, not yet decided. The two run rows the first
  backfill left open are closed as INTERRUPTED (`scripts/close_runs.py`)
- Done 2026-10-23: **the pricing workbook is a source**
  (`reference.apexus.pricing`, the `apexus` sheet, 45,551 rows loaded, as-of
  from the workbook's modified stamp until it arrives in a dated folder);
  **the acquire step is built** (`acquire/`, `scripts/acquire.py`: mirror
  the upload container into a landing root, record each blob in
  `meta.blob`, run the backfill loop over it) and waits only for a data
  role on the container; **email alerts are built** (`observe/notify.py`,
  `--email` on the freshness check, the acquire pass mails its report)
  and wait only for the SMTP settings and recipients; the freshness check
  gained `SPEC_CHANGED` (a spec edited after its last load, cleared by a
  `retype.py` verdict recorded in `meta.spec_check`) and `NEW_COLUMNS`
  (vendor columns no spec column claims, as a note). The payments versus
  services question is answered (`docs/data-findings.md`, 17)
- 2026-10-23, later: **hosting built and paused**. `Dockerfile` and
  `deploy/deploy.sh` make two scheduled Container Apps jobs (acquire every
  half hour, freshness daily); the registry and environment exist in
  `sbh-data-bank-rg`, the jobs do not. The developer wants the loaded data
  verified against the raw files first (`scripts/verify_load.py`); nothing
  in Azure changes until they say so. **Deployed 2026-11-03** on the
  developer's go: the container role on their account, then both jobs
  from image `fa309ec`, each with its own managed identity; the first
  hand-started acquire run read the container as the job identity and
  found nothing new, the first freshness run mailed its findings and
  exited 1 as designed (`deploy/README.md`). Every Azure change still
  waits for the developer's word. The reconciliation question (can
  Curve Monthly Services be reconciled with WeInfuse dispenses) is
  answered with numbers in `docs/data-findings.md`, 18: yes, on patient
  number, day and HCPCS, with a code crosswalk as the first rule
- 2026-10-23, verified: **the database matches the raw files**
  (`scripts/verify_load.py`, `docs/data-findings.md` 19): 208 files
  loaded, every line count matched, 0 missing, 0 double-current
  identities, 0 quarantined. The in-file duplicates are explained, and
  three of them are rule fixes waiting for D18, led by Monthly Payments'
  key, which needs `transtype`
- 2026-11-03: **the first upload through the container is loaded**. A
  colleague uploaded eighteen files over SFTP (every weekly source for
  09/29 to 11/02 plus Monthly Payments for October); the data role is
  still not granted, so they were mirrored once with the Azure CLI into
  the landing root under a dated folder and committed with the backfill
  loop: 18 LOADED, 0 refused, 0 quarantined, 0 conflicts, 1,300 corrections
  where the pulls overlap, verified against the landed files. The upload
  had no dated folder (the guide now says so), the Cardinal workbook had
  no summary sheet (that source is now the stale one), and three vendors
  added columns (Curve appointments 11, Curve services 1, PRxP 5), kept in
  `extra_cells` and reported as `NEW_COLUMNS`. Alerts went by mail both
  times the check ran; SMTP settings are in `.env`
- Done 2026-11-04: **the write side of the replay is built** (D18,
  `commit/rebuild.py`, `scripts/rebuild.py`). A rebuild types every
  loaded file again from `landed`, replaces the typed rows, runs the
  dedup rule over the whole source in memory and reconciles the live
  curated rows in SQL: kept where the new rule still produces the row,
  otherwise retired as `retyped` and the replacement inserted beside it.
  One transaction per source; refused whole when a line would newly
  quarantine or newly type, a file no longer loads, totals disagree or
  the replay conflicts. Without `--apply` it reports the same counts.
  First use: `transtype` joined the Monthly Payments key, and the plan
  says 41 files, 1,309,804 lines, 0 rehashed, 1,309,804 retired and
  1,309,804 inserted, exactly the prediction. The apply waits for the
  developer's go and runs in Azure as a one-off job execution
  (`scripts/job.sh rebuild <key>`). Queued then: the three Curve money
  columns typed as text (done the same night, below), one key question
  answered without the vendor (ABC, below) and one that turned out to be
  a vendor duplicate (Wellpartner, below). Same day: **the alert
  mail is laid out for people** (`observe/notify.py`): the same
  findings as HTML, one card per finding with a plain label ("Stale
  source", "New vendor columns"), alerts before notes, vendor columns
  as chips; the text part is unchanged and is what the terminal shows
- 2026-11-04, evening: **the sixteen columns three vendors added are
  claimed** (task 4, on the developer's go): Curve appointments eleven,
  Curve services `AllDxCodes`, PRxP five. Each typed from its sampled
  values, none required, so the files loaded before them still bind
  with the columns absent. The migration gained the rule it had claimed
  to have: a spec column missing from a live `landed`, `typed` or
  `curated` table is added, nullable, by a guarded batch per table. The
  rebuild of the three sources (every row rehashes when a column joins
  the hash, and the keyless appointments source changes every identity)
  runs in Azure after the Monthly Payments one; counts in
  `docs/decision-record.md`, "The columns three vendors added"
- 2026-11-04, night: **the ABC item number joins the purchases key**
  (developer: "Go ahead with Abc implementation"). Every within-file
  collision on (account, invoice, ndc) was a line with no NDC, the item
  number separates all of them and never points at two NDCs, so the key
  is unique in every loaded file without asking the vendor. Rebuilt from
  the laptop in 49 s: 5,746 live rows retired as `retyped` and 5,746
  inserted, 0 identities with two current rows, totals equal. The
  Wellpartner pairs are the same invoice written twice (one file,
  adjacent lines, same purchase order and amount, a trailing space in
  the entity name), a question for the vendor and not a key change. The
  same evening **the Monthly Payments rebuild execution failed on the
  database's 32 GB size quota** inside the retire UPDATE and rolled
  back: nothing written. Monthly Payments is 14.4 GB over its three
  layers and a rebuild keeps every retired row, so the quota has to
  grow before the next attempt, an Azure change for the developer
  (`docs/decision-record.md`, "The ABC item number joins the key")
- 2026-11-04, late: **a column type change is the rebuild's job**
  (developer: "Update the curve money column first"). The migration
  never alters a live column, and the 398,440 stored Curve money values
  would not convert in SQL without a second parser, so the rebuild now
  compares the live type of every business column with the spec's,
  nulls and alters the ones that differ in `typed` and `curated`,
  replaces typed from landed as before, and refills every curated row,
  history included, from the rebuilt typed row of its own line (a row
  with no typed line refuses the source). `totpmt`, `totadj` and
  `totref` are currency in the spec; the plan from the laptop: 7 files,
  398,440 lines all rehashed, three columns retyped, 280,707 live rows
  retired and reinserted, 389,897 curated rows refilled. The first
  apply in Azure failed on the size quota again (nothing written): the
  data file was at its 32 GB with 23.9 GB in tables and 8.6 GB still
  held by the persistent version store of the aborted Monthly Payments
  rebuild. **The developer raised the maximum size to 128 GB** ("We can
  increase the db size since its only 14.72 dollars"), and the three
  task-4 rebuilds then ran overnight on image e978868 as one execution:
  appointments 369,404 lines, 358,891 retired and inserted (25 min);
  services 398,440 lines, 280,707 retired and inserted, 389,897 rows
  refilled, the three columns now DECIMAL(38,4) in the catalog (91
  min); PRxP 9,695 lines, 7,692 retired and inserted (44 s). Read back
  2026-11-05: typed rows equal the lines, 0 identities with two current
  rows, and the three money totals of the current rows equal those of
  the retired ones to the cent. **The Monthly Payments rebuild started
  2026-11-05 16:53 UTC** on the enlarged file. The scheduled jobs still
  run c325653: the assistant's `az containerapp job update` was refused
  as a production deploy, so the developer moves them
  (`deploy/README.md`) before the next upload. The storage question is
  measured in `docs/decision-record.md`, "Where the bytes go": page
  compression would take Monthly Payments from 14.4 GB to 5.3,
  columnstore on `landed` further, and the retired rows a rebuild keeps
  are the real multiplier. Compression is parked for now on the
  developer's word, the size being 128 GB
- 2026-11-05: **the pricing source is a snapshot, and reads Apexus's
  own export.** The developer uploaded this quarter's prices as the
  CSV Apexus exports (`inbound/11052026/apexus/`, 52,518 rows, the key
  unique) rather than the analysts' workbook, with the instruction
  that the old quarter must go stale. The spec gained a second variant
  for the export (every column onto a target the sheet already feeds),
  `event_date: as_of`, `coverage: as_of` and `vanish: true`, and both
  stores learned to seed a snapshot's vanish candidates by `as_of`
  (before, only a coverage column could, so a snapshot vanished
  nothing); an ambiguous header under one variant's read settings now
  means "not this variant" instead of a crash. Loaded from the laptop
  after an in-memory replay said the same: 14,903 inserted, 37,615
  corrected, 7,936 vanished, 0 quarantined; the current pricing rows
  are exactly the new file, 52,518 as of 2026-11-05. Same day: the
  three task-4 rebuilds read back clean, the Monthly Payments rebuild
  started on the 128 GB file, both jobs moved to e978868 on the
  developer's word, image e7a5161 (the pricing variant) built and
  waiting for the developer to move them again, and every file under
  the OneDrive "Original Files" folder is read-only, after three
  workbooks were re-saved by Excel while open (same data, different
  bytes; `docs/decision-record.md`, "The pricing source is a snapshot")
- 2026-11-05, evening: **the Monthly Payments rebuild is applied.**
  `transtype` is in the key of the loaded data. The execution on image
  e978868 ran 2 h 40 min on the 128 GB file (16:54 to 19:34 UTC) and
  committed exactly the plan: 41 files, 1,309,804 lines, 0 rehashed,
  1,309,804 live rows retired as `retyped` and 1,309,804 inserted.
  Read back: landed lines, typed rows and current rows all 1,309,804,
  curated 2,619,608 in all, 0 identities with two current rows, and
  the `amount` and `fee` totals of the current rows equal those of the
  retired rows and of `typed` to the cent (194,205,717.26 and
  1,106,575,137.32). Every D18 rebuild queued since 2026-11-04 is now
  in Azure; `docs/decision-record.md`, "The Monthly Payments rebuild
  lands". Both jobs moved to e7a5161 the same evening on the
  developer's word ("Go ahead and move the images yourself"), so the
  half-hourly run reads the Apexus CSV shape from here on. 2026-11-06:
  the older image tags deleted on the developer's word; `latest`
  shared e7a5161's manifest and took it along, so the image was
  rebuilt from the commit's tree and the deploy script now tags each
  build once (`docs/decision-record.md`, "The registry keeps one tag
  per build"). Open: the freshness `NEW_COLUMNS` notes on the three
  task-4 sources persist because a rebuild re-binds the recorded
  header but does not re-record it in `meta.source_file`; fix proposed
- 2026-11-06, later: **the presentation layer exists** (developer:
  readers "want the data first not the audit columns"; "I want it to
  be fast"). SQL Server cannot reorder columns, and rewriting the
  tables would cost a Monthly Payments-sized job for no reader's
  benefit, so `pres` holds two generated views per source over
  `curated`: `pres.<source>` is the current rows with the business
  columns first in spec order and only as-of, file id and line number
  after them, the dedup rule applied; `pres.<source>_history` is every
  version with the ledger columns a reader can use at the end. Views
  hold no data. Applied to Azure the same day (42 views, 277
  statements, all guarded); Power BI connects to `pres` in import
  mode. The 19 empty `raw.*` tables were dropped the same day
  (`docs/decision-record.md`, "The presentation layer")
- 2026-11-06, evening: **the curated layer is verified against the
  staging layers** (developer: "make sure the data is good to use
  before presenting it to the management"). `scripts/verify_layers.py`
  holds every source's curated rows against `landed` and `typed`
  inside the database: line accounting, completeness both ways against
  what the batches recorded, duplicates, the row hash and every
  business column value-equal on the current generation, numeric
  totals reconciled; results to a JSON log, counts and line numbers
  only. Its first run found one defect, in the engine's bookkeeping
  and not in any value: a rebuild left the history rows it keeps in
  place with the hash of the spec that wrote them, so 109,190 Curve
  services and 1,482 PRxP superseded rows carried a stale fingerprint.
  The rebuild now rehashes those rows, counted the same by the plan
  and the apply, and copies every business column with the hash (PRxP
  had 847 superseded rows refilled with its five new columns null);
  the rows already in Azure were repaired on the developer's word
  (`scripts/refresh_history.py`). What a rebuild would have made
  history inside the replay (a line a newer delivery corrected) is not
  written, by D18's design, and the check counts those lines
  separately (PRxP 162). All 21 sources pass; the table is in the
  decision record, "The results, source by source". Same evening: Jay
  answered that Cardinal no longer produces the "Cardinal_Invoice"
  summary report and had been making it by hand from the Invoice
  Review, so `wholesaler.cardinal.invoice_summary` is `discontinued`
  (its 16,004 rows stay; its silence is not an alert; the detail is
  the Cardinal purchase source). Results in `docs/decision-record.md`,
  "Verified for management"
- 2026-11-07: **Curve changed the Monthly Payments export.** The two
  files Jay uploaded 2026-11-06 (05/01 to 09/30 in two pulls, in a
  dated folder) were held by the midnight acquire run with every row
  quarantined: `transmonth` and `postmonth` now arrive as "Jul-26"
  where 41 loaded files wrote "Jul 2026". Jay uploaded the download
  untouched, so the spec declares the hyphenated two-digit-year shape
  beside the two it had (a month name is never ambiguous; nothing
  numeric is admitted; the column stays DATE). The dry run loads the
  held file whole. Loading the two files waits for the developer's
  word: image, jobs, then a load by hand from the laptop copies or a
  re-upload, since a blob with a batch is never fetched again
  (`docs/decision-record.md`, "Curve changed the Monthly Payments
  export"). **Loaded 2026-11-07** by the 01:00 UTC acquire run after
  Jay re-uploaded both files to `inbound/11072026/`: 118,497 and
  109,451 rows, 83,031 inserted, 57,397 unchanged, 87,520 corrected, 0
  conflict, 0 quarantined; Monthly Payments is current through
  2026-09-30 with 1,392,835 current rows. 83,683 of the corrections
  differ only in `dob`, a text column the new export zero-pads, and
  9,054 rows of the Jul-Sep file carry `reference` in scientific
  notation where the old export wrote 15 digits: a vendor defect to
  raise with Curve. Next, in order (also in the assistant's memory):
  close the run row the stopped laptop load left (`close_runs.py`),
  verify Monthly Payments, decide `dob` as a date, let a held file be
  retried daily (`acquire/ledger.py`), re-record headers on rebuild
- 2026-11-09: **after the restart.** The run row is closed and the
  freshness check reports 0 alerts. Monthly Payments `dob` is a `date`
  (developer: "yes DOBs must be date column"; Jay uploads the report as
  it comes, so every vendor shape change is handled in the spec or the
  code): the retype says 43 files, 1,537,752 lines, every line with a
  value rehashed, 0 newly quarantined, totals agree; the rebuild runs in
  Azure on the image built from this commit. The `NEW_COLUMNS` note now
  binds the recorded header against the spec as it is now instead of
  reading the recorded target map, which is the landed layout the
  replay depends on and must not be rewritten; the three stale notes
  cleared with nothing written. The registry holds one tag, the one
  the jobs run (`docs/decision-record.md`, the two 2026-11-09 entries)
- 2026-11-10 to 11: **the `dob` rebuild landed on the second attempt.**
  The first apply was killed at the job's four-hour limit and rolled
  back whole; the database's data IO, not the container, is what it
  waited on (two vCores, read IO at its cap). With the limit at twelve
  hours for the run (back at four since) the second apply wrote the
  plan's counts in 9 h 58 min: 44 files, 1,549,535 typed rows replaced,
  kept 43, retired and inserted 1,404,575, refilled 2,801,942, history
  rehashed 87,520. `dob` is a DATE in `typed`, `curated` and the `pres`
  views (the migration re-created them); both jobs run `50bef75`, the
  only tag in the registry. `verify_layers.py` asks its completeness
  question in stages now (it had passed the one-hour timeout twice) and
  **fails Monthly Payments on history, not on the current rows**:
  64,673 rows superseded as `corrected` on 2026-11-07 sit beside the
  current row of their own line, because under a date those
  corrections never happened and the rebuild made the earlier line
  current again without retiring its old history row. The current
  layer reads back clean (0 double identities, 0 hash or `dob`
  differences). Whether those rows are retired as `retyped` or
  reported as residue is the developer's decision
  (`docs/decision-record.md`, "The `dob` rebuild lands on the second
  attempt"). The 2026-11-11 freshness mail: six sources stale at nine
  days (ABC, Cardinal invoice, PRxP, PharmaForce, both WellPartner),
  none of them in the 2026-11-09 upload
- 2026-11-17: **the stale history is retired and a held file is
  retried daily.** The developer chose to retire the 64,673 rows: the
  rebuild now retires any history row whose line its rule makes
  current (`history_retired`), and `scripts/retire_stale_history.py`
  did the same once for Monthly Payments, exactly 64,673 rows, all
  from the 2026-11-07 load, no live row touched. `acquire/ledger.py`
  counts a blob version as seen only when a batch settled it or for a
  day after its last fetch, so a held or refused file comes back daily
  and a corrected spec is enough to load it; in force once the jobs
  run an image with it. The 2026-11-15 upload loaded on 2026-11-16 (10
  files, all `LOADED`), again without the six sources the freshness
  mail has listed daily since 2026-11-11, now fifteen days stale
  (`docs/decision-record.md`, "A rebuild retires the history its own
  rule undoes")
- 2026-11-17: **six sources are paused.** Jay's upload sheet marks ABC,
  Cardinal invoice review, PRxP, PharmaForce and both WellPartner
  reports "no data" for 11/02 to 11/15: the 340B program is halted,
  for a few months. Their specs say `lifecycle: paused`, which silences
  the stale alert and nothing else; a file that arrives still loads,
  and the freshness check then notes the source delivered so it can be
  made active again (`docs/decision-record.md`, "Six sources are
  paused")
