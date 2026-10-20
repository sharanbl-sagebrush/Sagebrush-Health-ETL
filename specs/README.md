# Source specs

One YAML file per vendor extract. A spec says how to recognise a file, how to
read it, what its columns mean, how to type them, what identifies a row, and
what to do when the vendor changes something. The model that validates them is
`src/sbh_data_bank/registry/spec.py`; this page is the one-line version of
every field in it.

Specs are strict: an unknown key, a date column without formats, a `recognise_only`
variant without `superseded_by`, or an empty natural key without `notes` is a
startup failure, not a setting that silently does nothing.

## Field glossary

### SourceSpec — the top level of every file

| Field | Meaning |
|---|---|
| `key` | Stable identifier, `<group>.<vendor>.<report>`. Never changes; the raw table, batches and history are keyed on it. |
| `name` | Human-readable name for logs, alerts and reports. |
| `vendor` | Who produces the file. |
| `group` | The delivery folder the file arrives in, e.g. `"1 Wholesalers"`. |
| `catalog_names` | What the finance team calls this feed in their source catalogue (`"R and S"` is McKesson Custom). |
| `lifecycle` | `active` sources are expected to deliver, so silence alerts. See `SourceLifecycle`. |
| `filename_patterns` | Case-insensitive regexes over the filename. A routing hint only; the variant detector decides what a file is. |
| `period_from_filename` | Regex with named groups `year` and `month`, for sources delivered one file per period. Only Monthly Payments uses it. |
| `variants` | The shapes this file can take, each with its own detector, read settings and column map. At least one. See `SchemaVariant`. |
| `natural_key` | Target columns that identify one business row. Non-empty selects natural-key dedup; empty falls back to the row hash and makes `notes` mandatory. |
| `constants` | Values injected into every row. One Pharmaforce spec serves two sites this way. |
| `on_new_column` | Drift policy when the vendor adds a column. Default `warn`. See `DriftPolicy`. |
| `on_missing_column` | Drift policy when an expected column vanishes. Default `fail`. |
| `expected_cadence_days` | Staleness threshold for freshness alerting ("alert if nothing seen in N days"), not a schedule. `null` means irregular. Nothing consumes it yet. |
| `coverage_start` | ISO date before which the source has no data, so absence before it is not a gap. |
| `event_date` | The date column that says when the business event happened (invoice date, fill date), or `as_of` for a snapshot source whose rows carry no date. Must be a `date` column in every loading variant. Enables the as-of floor, coverage and vanish. Decision D1. |
| `coverage` | Where a delivery's covered date window comes from: `filename` (default), `data` (min and max of the coverage column) or `as_of` (everything up to the as-of; mandatory for a snapshot). Decision D2. |
| `coverage_column` | The date column the vendor's report filter runs on when it is not the event date (Wellpartner claims filter on processed date). The window is compared against it. |
| `coverage_start_offset_days` / `coverage_end_offset_days` | Days added to the resolved window's ends. Cardinal ends one day before its as-of (`-1`); WeInfuse starts a day early (`-1`). |
| `vanish` | Whether a current row a newer, covering delivery no longer carries is superseded as `vanished`. Omitted: yes when `event_date` is declared, no otherwise. `false` for patients and Pharmaforce. Decision D3. |
| `vanish_scope` | Columns naming the slice a delivery speaks for beyond its dates, so a McKesson file for one account never retires another's rows. Requires vanish. |
| `row_hash_exclude` | Columns stored but left out of the row hash because the vendor recomputes them at extraction time (reference prices, balances). Never a key column, the event date or a control total. Decision D6. |
| `as_of_source` | Overrides where the as-of comes from, for backlog files only: `xlsx_created`, `xlsx_modified`, `filename` or `operator`. Omitted, the standard chain applies: dated upload folder, xlsx creation stamp, filename end date, operator date. Decision D5. |
| `notes` | Free text. Mandatory for a keyless spec, where it must record which key candidates were tested and why none is unique. |

### SchemaVariant — one shape a file can take

| Field | Meaning |
|---|---|
| `name` | Identifier for the variant, unique within the spec. Recorded on every batch. |
| `detect` | How to recognise this shape from the header row. See `VariantDetector`. |
| `read` | How to find the header and data rows. See `ReadSpec`. Omit for a plain CSV with the header on row 0. |
| `columns` | One `ColumnSpec` per target column. At least one; targets must be unique. |
| `action` | `load` (default) or `recognise_only`. See `VariantAction`. |
| `superseded_by` | Name of the variant that replaces this one. Required with `recognise_only`, forbidden with `load`. |
| `description` | Why this variant exists. |

Every variant must declare every natural-key column, so a file of the wrong
shape cannot land keyless and duplicate on re-delivery.

### VariantDetector — recognising a shape by its headers

| Field | Meaning |
|---|---|
| `require_all` | Every one of these headers must be present (compared after normalisation). |
| `require_none` | None of these may be present. This is what separates the 9-column Cardinal summary from the 17-column detail: the summary is the one with no `NDC`. |

A detector must list at least one of the two. At bind time exactly one
variant must match; zero raises `NoVariantMatchedError`, several raise
`AmbiguousVariantError`. There is no default and no fallback.

### ReadSpec — finding the header and the rows

| Field | Meaning |
|---|---|
| `sheet` | Worksheet to read, matched case-insensitively. `None` takes the first sheet. |
| `sheet_pattern` | Regex selecting several worksheets, all read and concatenated. Requires `sheet_name_column`. |
| `sheet_name_column` | Target column that receives the worksheet name each row came from. The only column allowed to have no `sources`. |
| `preamble_lines` | Fixed number of lines to discard before the header. Athena reports have exactly one. |
| `header_marker` | A column name that identifies the header row, scanned for from the top. Use when the preamble length varies (ABC). Mutually exclusive with `preamble_lines`. |
| `max_preamble_scan` | How many rows to scan for `header_marker` before failing. Default 30. |
| `encoding` | Default `utf-8-sig`, which also strips a BOM. |
| `delimiter` | Default `,`. |
| `skip_trailing_blank_rows` | Default `true`. Drops fully empty rows at the end of the file. |
| `footer` | `none` (default) or `totals_row`. See `FooterPolicy`. |

### ColumnSpec — one column

| Field | Meaning |
|---|---|
| `target` | Canonical destination name, lower snake case. Stable even when the vendor renames. |
| `sources` | Accepted vendor headers in preference order, matched on a normalised form (case, punctuation and whitespace folded; nothing fuzzier). |
| `type` | How the raw text is parsed. See `FieldType`. Default `text`. |
| `required` | The column must exist in the file **and** every row must have a value. Column absent is drift; value absent quarantines that row. |
| `date_formats` | `strptime` formats tried in order. Mandatory for `date`, forbidden otherwise. Never inferred. |
| `max_length` | Text only. An over-length value fails rather than truncates. |
| `pii` | How the value is stored. See `PiiClass`. Default `none`. |
| `on_error` | What one unparseable cell does. See `OnError`. Default `quarantine`. |
| `control_total` | The column is additive and is summed on load as a control figure, checked against the file's footer when it has one. Never for rates. |
| `description` | Why the column matters, kept next to the mapping. |

### Enums

**FieldType** — `text`, `integer`, `decimal`, `currency` (Decimal, never float,
understands `($40.00)` as negative), `date`, `boolean`, `ndc` (normalised to
11 digits, resolved against the NDC directory when the source gives an
unhyphenated 10-digit code, quarantined when it cannot be).

**PiiClass** — `none` (stored in clear), `join_key` (internal identifier such
as PatNum; in clear because reconciliation joins on it), `quasi` (indirect
identifier; in clear, risky only in combination), `direct` (name, DOB,
address, phone; encrypted at rest with access audit), `prohibited` (never
lands anywhere; matched so drift checks see it, then discarded).

**OnError** — `quarantine` (default; the row goes to quarantine with a reason
code), `null` (value becomes null, row proceeds; must be chosen explicitly),
`fail` (abort the whole file).

**DriftPolicy** — `fail` (stop until a human has looked), `quarantine_file`
(land nothing, keep the file, raise for triage), `warn` (load and record the
drift).

**VariantAction** — `load` (read it and land the rows), `recognise_only`
(identify the file, land nothing; for a report superseded by a better one that
covers the same ground).

**FooterPolicy** — `none` (no footer expected; a trailing row that looks like
one still quarantines), `totals_row` (the last row is a totals line; its
figures are checked against our sums of the `control_total` columns).

**SourceLifecycle** — `active`, `discontinued` (vendor stopped producing it;
history stays, absence is not an alert), `no_access` (known to exist, not
obtainable), `planned`.

## How a file becomes rows

1. **Registry** — `registry.loader.load_spec_dir` reads every `.yaml` here
   into a validated `SourceSpec` and refuses duplicate keys.
2. **Reader** — `ingest.reader.open_source` opens the CSV or worksheet, finds
   the header by `preamble_lines` or `header_marker`, trims trailing empty
   header cells, and streams raw rows.
3. **Header index** — `registry.headers.HeaderIndex` normalises every vendor
   header and drops export artifacts (`Unnamed: 0`, `Textbox42`). Two headers
   that normalise to the same thing are an error, never a guess.
4. **Variant selection** — `registry.binding.select_variant` tests each
   variant's detector against the header set. Exactly one must match.
5. **Binding** — `registry.binding.bind` maps each `ColumnSpec` to a file
   position via its `sources`, and reports new and missing columns so the
   caller can apply the drift policy.
6. **Load** — `ingest.load.load_file` streams every row through
   `ingest.reader.type_row`: each cell is parsed per its `type`, failures
   quarantine the row with reason codes, prohibited values are dropped,
   required blanks quarantine. The loader keeps row counts, row hashes and
   control-total sums, recognises a `totals_row` footer and checks the
   vendor's figures against ours. The result is a `FileLoad` that is either
   `ok` with every row accounted for, or carries the reason it is not.
7. **Commit** — `commit.engine.CommitEngine` writes `meta.source_file`,
   `meta.batch`, the `landed.*`, `typed.*` and `curated.*` rows and
   `stg.quarantine` in one transaction, applying the single dedup rule
   (`curate/rule.py`; `store/schema.py`, `DEDUP_RULE`; CLAUDE.md,
   "Duplicates"): identity is the `natural_key`, or every column when there
   is none, plus an occurrence number, and a newer delivery supersedes an
   older one by as-of date. A duplicate file, a load that is not ok, a
   missing as-of, an as-of floor violation, an unknowable window, a window
   the data contradicts or a conflict leaves the file and batch records and
   nothing else.

## Where the specs came from

They were generated once by `tools/spec_build/` from the real header rows of
the extracts, then hand-edited and verified by loading every real file. The
generators are kept as a record of how the column lists were derived, not as
the source of truth. **Re-running them overwrites these files**, including
every hand-written comment, so edit the YAML directly.
