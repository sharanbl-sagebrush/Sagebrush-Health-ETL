"""The layered schema, and the DDL generated from source specs.

Four layers, in the medallion pattern (decided 2026-10-07; reasoning in
docs/architecture-and-plan.md, "Storage layers"), plus the metadata and
staging schemas that describe what happened:

``meta``     what happened - files seen, batches, runs.
``stg``      what did not type - quarantined rows and duplicate findings.
``landed``   every cell as delivered, as text, one row per file line. Written
             once from the reader's raw rows and never changed. Prohibited
             columns (SSN) are dropped at this boundary; nothing else is.
``typed``    the landed row parsed per the spec, plus its row hash. Rebuilt
             from landed whenever a parsing rule changes. A row that will not
             parse goes to ``stg.quarantine`` instead.
``curated``  the ledger: which typed rows are current, which were corrected,
             which arrived late, under the dedup rule in force. Rebuilt from
             typed whenever a business rule changes.
``pres``     views and marts over curated for users and reconciliation. The
             schema is created here; its views are not generated from specs.

Every table in the curated layer is append-only and bitemporal. Nothing is
ever updated in place and nothing is ever deleted. A correction is a new row
plus a marker on the old one, so the question "what did we believe on 1
August" stays a lookup rather than a reconstruction - and the
delete-by-date-range pattern in the previous backfill scripts, which could
remove rows belonging to other files, becomes impossible to express.

Current state is ``superseded_by IS NULL``. State as of a past moment is
``created_at <= @t AND (superseded_at IS NULL OR superseded_at > @t)``.

Every generated table carries the same four audit columns (decision D11,
docs/decision-record.md): ``created_at`` and ``created_by`` say which load
wrote the row; ``updated_at`` and ``updated_by`` are always null on landed and
typed, which are immutable, and are set on a curated row only when it is
superseded, beside ``superseded_by``.

DUPLICATE HANDLING is one rule for every source, named by :data:`DEDUP_RULE`
and described in CLAUDE.md, "Duplicates": identity = the spec's identity
columns (:func:`identity_columns_for`) plus an occurrence number in file
order, and supersession ordered by each delivery's as-of date (when the vendor
extracted it, carried on ``meta.source_file``), never by arrival. The previous
pipeline MERGEd four tables on keys that were not unique; this design never
collapses two rows of one file, whatever the key.

How this fits
-------------
This module produces T-SQL text; it never executes it. ``scripts/migrate.py``
is the only caller: it takes ``meta_statements()`` once, then
``table_statements(spec)`` for every spec in ``specs/``, and runs each
statement through ``store.connection.Database``. Every statement is guarded
(``IF OBJECT_ID(...) IS NULL``, ``IF COL_LENGTH(...) IS NULL``, ``IF NOT
EXISTS (... sys.indexes ...)``) so the migration is idempotent and safe to
re-run, and a column added to a live metadata table is a guarded ``ALTER``
rather than a hand-run script.

The first schema (24 tables on Azure, CLAUDE.md) put typed, deduplicated
rows straight into ``raw.*``. Those tables predate the layered decision,
were never written to (the commit engine did not exist), and are not touched
by this module; dropping them is a separate, explicit step.

``sbh_data_bank.commit`` writes into these tables: ``commit.sql`` builds its
INSERT column lists from the same spec properties this module builds the DDL
from, and ``tests/unit/test_commit.py`` holds the two against each other.
"""

from __future__ import annotations

from sbh_data_bank.registry.spec import RESERVED_COLUMN_NAMES, FieldType, PiiClass, SourceSpec
from sbh_data_bank.transforms.values import CURRENCY_PLACES, QUANTITY_PLACES

__all__ = [
    "CURATED_LEDGER_COLUMNS",
    "DEDUP_RULE",
    "LAYERS",
    "LEDGER_COLUMN_NAMES",
    "curated_statements",
    "identity_columns_for",
    "landed_statements",
    "meta_statements",
    "table_name",
    "table_statements",
    "typed_statements",
]

#: The three per-source layers this module generates tables for, in build
#: order. ``pres`` holds views, not generated tables, and is not listed.
LAYERS = ("landed", "typed", "curated")


def _index(name: str, table: str, definition: str) -> str:
    """A guarded CREATE INDEX.

    T-SQL has no ``CREATE INDEX IF NOT EXISTS``; that is Postgres. Re-running a
    migration must be safe, so every index is guarded on sys.indexes.

    ``definition`` is the parenthesised column list plus any ``INCLUDE`` or
    ``WHERE`` clause, pasted verbatim after the table name.
    """
    return (
        f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = '{name}' "
        f"AND object_id = OBJECT_ID('{table}'))\n"
        f"    CREATE INDEX {name} ON {table} {definition}"
    )


def _add_column(table: str, column: str, definition: str) -> str:
    """A guarded ALTER TABLE ... ADD, for columns added after a table went live.

    ``CREATE TABLE`` is guarded on the table's existence, so a column added to
    ``_META_TABLES`` later never reaches a database that already has the
    table. Each such column is therefore also emitted this way; on a fresh
    database the guard sees the column and does nothing.
    """
    return (
        f"IF COL_LENGTH('{table}', '{column}') IS NULL\n"
        f"    ALTER TABLE {table} ADD {column} {definition}"
    )


#: The dedup rule in force, recorded on ``meta.batch.dedup_mode`` and on every
#: curated row, so the policy a load ran under is part of the record. Bump
#: the version when the rule changes; the curated layer keeps prior results
#: under the old version.
#:
#: The rule (decided 2026-10-15; CLAUDE.md, "Duplicates"; reasoning in
#: docs/architecture-and-plan.md, "Duplicates and delivery order"):
#:
#:   identity   = identity_columns_for(spec) + occurrence number, where rows
#:                in one delivery that share identity columns are numbered
#:                1, 2, 3 in file order. Nothing within a file is collapsed.
#:   ordering   = each delivery's as-of date (vendor extraction time), never
#:                arrival time. Newer as-of supersedes; older is history only;
#:                equal as-of with different values holds the batch.
#:   outcomes   = inserted | unchanged | corrected | late | conflict | vanished
#:
#: One rule replaces the three modes (natural_key, row_hash, file_position)
#: the first schema carried: a keyed source is the key case, a keyless source
#: is the all-columns case, and Allscripts eRx's 8,100 identical rows are
#: 8,100 occurrences rather than an exception.
DEDUP_RULE = "key_occurrence_asof.v1"


def identity_columns_for(spec: SourceSpec) -> tuple[str, ...]:
    """The columns that identify a business row for this source.

    The spec's natural key when it declares one; otherwise every target
    column, since a keyless source (a denormalised report extract) has no
    smaller identity. Derived from the spec rather than declared separately,
    so it cannot drift from the key it depends on. The occurrence number is
    added at the curated layer, not here.
    """
    if spec.natural_key:
        return tuple(spec.natural_key)
    return tuple(target for target, _kind, _max in _all_targets(spec))


#: The ledger columns that open every curated.* table, as a DDL fragment.
#: Column by column:
#:   tenant_id         owner; non-null so row-level security can key on it
#:   batch_id          the meta.batch that wrote the row (transaction identity)
#:   source_file_id    the meta.source_file it came from
#:   line_number       position in that file, so the row can be found again
#:                     in landed and typed, which share this (file, line) key
#:   dedup_rule        the DEDUP_RULE this row was curated under
#:   identity_hash     SHA-256 over the identity columns' typed values
#:   occurrence        1-based position among rows of this delivery that
#:                     share identity_hash, in file order
#:   row_hash          SHA-256 over every typed column (content identity)
#:   as_of             the delivery's as-of date, copied from
#:                     meta.source_file so supersession never needs a join
#:   outcome           how the row entered: inserted | corrected | late
#:                     (unchanged rows write nothing here; the batch counts them)
#:   created_at        transaction time: when the warehouse learned of it
#:   created_by        the identity that ran the load, from the batch
#:   superseded_by     batch_id of the batch that retired it; NULL = live
#:   superseded_at     when that happened; NULL = live
#:   supersede_reason  corrected (a newer version arrived) | vanished (a newer
#:                     delivery covering its date no longer carried it) |
#:                     late (written already superseded: an older delivery's
#:                     version, behind the batch named in superseded_by)
#:   updated_at /      set with the superseded_* columns and never otherwise,
#:   updated_by        since supersession is the only update a ledger row has
#: Unchanged rows (same identity, same hash, newer as-of) write nothing here;
#: they are counted on the batch. (batch_id, source_file_id, line_number) is
#: the primary key, so one line of one file lands once per batch.
CURATED_LEDGER_COLUMNS = """    tenant_id        NVARCHAR(64)   NOT NULL,
    batch_id         NVARCHAR(64)   NOT NULL,
    source_file_id   BIGINT         NOT NULL,
    line_number      INT            NOT NULL,
    dedup_rule       NVARCHAR(40)   NOT NULL,
    identity_hash    CHAR(64)       NOT NULL,
    occurrence       INT            NOT NULL,
    row_hash         CHAR(64)       NOT NULL,
    as_of            DATE           NOT NULL,
    outcome          NVARCHAR(16)   NOT NULL,
    created_at       DATETIME2(3)   NOT NULL,
    created_by       NVARCHAR(128)  NOT NULL,
    superseded_by    NVARCHAR(64)   NULL,
    superseded_at    DATETIME2(3)   NULL,
    supersede_reason NVARCHAR(16)   NULL,
    updated_at       DATETIME2(3)   NULL,
    updated_by       NVARCHAR(128)  NULL"""

#: The audit columns of the two immutable layers (decision D11). The updated
#: pair exists so every table has the same four, and is always null here.
_IMMUTABLE_AUDIT_COLUMNS = [
    "    created_at       DATETIME2(3)   NOT NULL,",
    "    created_by       NVARCHAR(128)  NOT NULL,",
    "    updated_at       DATETIME2(3)   NULL,",
    "    updated_by       NVARCHAR(128)  NULL,",
]

#: Every non-business column a generated table can carry, so a reader can
#: separate the ledger from the vendor's columns without guessing. The spec
#: model owns the set, so it can refuse a vendor column with one of these
#: names before any DDL is generated.
LEDGER_COLUMN_NAMES = RESERVED_COLUMN_NAMES


#: The metadata and staging DDL, as one script that meta_statements() splits
#: on ';'. Column-level notes are SQL comments so they survive into the DDL
#: an operator sees. No ';' may appear inside a comment, or the split breaks.
_META_TABLES = """
IF SCHEMA_ID('meta')    IS NULL EXEC('CREATE SCHEMA meta');
IF SCHEMA_ID('stg')     IS NULL EXEC('CREATE SCHEMA stg');
IF SCHEMA_ID('landed')  IS NULL EXEC('CREATE SCHEMA landed');
IF SCHEMA_ID('typed')   IS NULL EXEC('CREATE SCHEMA typed');
IF SCHEMA_ID('curated') IS NULL EXEC('CREATE SCHEMA curated');
IF SCHEMA_ID('pres')    IS NULL EXEC('CREATE SCHEMA pres');

-- One row per physical file ever seen. Identity is the content hash, so the
-- same extract delivered under a different name is recognised as already
-- processed, and a corrected file under the same name is not.
--   source_key     which spec claimed the file
--   file_name      as delivered - a routing hint only, filenames overstate
--                  their date coverage (Cardinal "01012023-..." has no 2023)
--   file_sha256    the identity
--   sheet_name     one row per worksheet for a workbook, NULL for a CSV
--   delivery       the drop or upload the file arrived in, if known
--   delivery_rank  ordinal of as_of among this source's deliveries, so a
--                  later re-pull can be told from an earlier one by one INT
--   first_seen_at  arrival time: when the pipeline first registered the
--                  file. Audit only - never used to order deliveries
--   as_of          when the vendor extracted the file. Orders deliveries
--                  (CLAUDE.md, "Duplicates"). Must not precede the latest
--                  event date in the file
--   as_of_source   where as_of came from, a registry.spec.AsOfSource value:
--                  folder (the dated upload folder, the rule going forward),
--                  xlsx_created or xlsx_modified (docProps/core.xml stamps
--                  written by the vendor portal), filename (end of the date
--                  range in the name), operator (supplied at upload). A file
--                  with none is refused, not guessed (decision D5)
--   coverage_from / coverage_to  the date window this delivery covers, per
--                  the spec's coverage source and offsets (decision D2). The
--                  vanish outcome runs inside it
--   header_json    the header row exactly as delivered and the target each
--                  header bound to, so landed.* can name columns by target
--                  without losing the vendor's spelling
IF OBJECT_ID('meta.source_file') IS NULL
CREATE TABLE meta.source_file (
    source_file_id   BIGINT IDENTITY(1,1) PRIMARY KEY,
    tenant_id        NVARCHAR(64)   NOT NULL,
    source_key       NVARCHAR(128)  NOT NULL,
    file_name        NVARCHAR(400)  NOT NULL,
    file_sha256      CHAR(64)       NOT NULL,
    file_bytes       BIGINT         NULL,
    sheet_name       NVARCHAR(128)  NULL,
    delivery         NVARCHAR(200)  NULL,
    delivery_rank    INT            NOT NULL DEFAULT 0,
    first_seen_at    DATETIME2(3)   NOT NULL,
    as_of            DATE           NULL,
    as_of_source     NVARCHAR(16)   NULL,
    coverage_from    DATE           NULL,
    coverage_to      DATE           NULL,
    header_json      NVARCHAR(MAX)  NULL,
    CONSTRAINT uq_source_file UNIQUE (tenant_id, source_key, file_sha256, sheet_name)
);

-- One row per attempt to load one file (or one worksheet of one file).
-- This is the persisted form of ingest.load.FileLoad.
--   run_id           the pipeline execution it belonged to (meta.run)
--   variant_name     which spec variant matched the header
--   dedup_mode       the DEDUP_RULE version in force for this load
--   outcome          a LoadOutcome string - only LOADED may commit rows
--   detail           why, for any outcome other than LOADED
--   rows_landed      file lines written to landed.* (everything, footer too)
--   rows_read        data rows handed to the typer (landed minus footer and
--                    blank lines)
--   rows_inserted    new identities written to curated.*
--   rows_superseded  prior rows this batch corrected (a newer version)
--   rows_unchanged   rows already current with the same hash, nothing written
--   rows_late        rows whose delivery as_of is older than the current
--                    row's - stored as history, current untouched
--   rows_quarantined rows that failed typing, now in stg.quarantine
--   rows_conflict    rows with the same as_of and a different hash from the
--                    current row. Any conflict holds the whole batch
--   rows_vanished    prior rows retired because a newer delivery covering
--                    their date no longer carried them (not part of the sum)
--   control_totals   JSON of column -> sum, and the footer check if any
-- inserted + superseded + unchanged + late + quarantined must equal rows_read
-- when the batch commits. The old pipeline reported promoted=0 on working
-- runs because it counted only inserts and not MERGE updates - hence one
-- counter per outcome, not one in total.
IF OBJECT_ID('meta.batch') IS NULL
CREATE TABLE meta.batch (
    batch_id          NVARCHAR(64)  NOT NULL PRIMARY KEY,
    tenant_id         NVARCHAR(64)  NOT NULL,
    run_id            NVARCHAR(64)  NOT NULL,
    source_key        NVARCHAR(128) NOT NULL,
    source_file_id    BIGINT        NULL,
    variant_name      NVARCHAR(128) NULL,
    dedup_mode        NVARCHAR(40)  NULL,
    outcome           NVARCHAR(40)  NOT NULL,
    detail            NVARCHAR(MAX) NULL,
    rows_landed       INT           NOT NULL DEFAULT 0,
    rows_read         INT           NOT NULL DEFAULT 0,
    rows_inserted     INT           NOT NULL DEFAULT 0,
    rows_superseded   INT           NOT NULL DEFAULT 0,
    rows_unchanged    INT           NOT NULL DEFAULT 0,
    rows_late         INT           NOT NULL DEFAULT 0,
    rows_quarantined  INT           NOT NULL DEFAULT 0,
    rows_conflict     INT           NOT NULL DEFAULT 0,
    rows_vanished     INT           NOT NULL DEFAULT 0,
    control_totals    NVARCHAR(MAX) NULL,
    started_at        DATETIME2(3)  NOT NULL,
    finished_at       DATETIME2(3)  NULL
);


-- One row per pipeline execution. A run groups the batches of one trigger
-- (a scheduled poll, a manual replay) so "what did Sunday's run do" is one
-- query.
--   trigger_kind  what started the run - scheduled, manual, replay
--   outcome       overall result once every batch has finished
IF OBJECT_ID('meta.run') IS NULL
CREATE TABLE meta.run (
    run_id       NVARCHAR(64)  NOT NULL PRIMARY KEY,
    tenant_id    NVARCHAR(64)  NOT NULL,
    trigger_kind NVARCHAR(64)  NOT NULL,   -- 'trigger' is a T-SQL reserved word
    started_at   DATETIME2(3)  NOT NULL,
    finished_at  DATETIME2(3)  NULL,
    outcome      NVARCHAR(40)  NULL,
    detail       NVARCHAR(MAX) NULL
);

-- Rows that would not parse, kept whole so they can be corrected and replayed
-- without going back to the source file. Never carries a prohibited column.
-- The persisted form of ingest.reader.QuarantinedRow. The row is also in
-- landed.* under the same (source_file_id, line_number), so the payload here
-- is a convenience for the operator, not the only copy.
--   line_number  where in the file, so an operator can look at the original
--   reasons      the distinct reason codes, comma-joined, for filtering
--   errors       JSON of every CellError (column, reason, raw value)
--   payload      JSON of target -> raw text for every known non-prohibited
--                column, which is enough to re-type the row after a fix
--   resolved_*   set when somebody has dealt with it, leaving the row in
--                place as a record. Open quarantine is resolved_at IS NULL.
IF OBJECT_ID('stg.quarantine') IS NULL
CREATE TABLE stg.quarantine (
    quarantine_id  BIGINT IDENTITY(1,1) PRIMARY KEY,
    tenant_id      NVARCHAR(64)   NOT NULL,
    batch_id       NVARCHAR(64)   NOT NULL,
    source_key     NVARCHAR(128)  NOT NULL,
    source_file_id BIGINT         NULL,
    line_number    INT            NOT NULL,
    reasons        NVARCHAR(400)  NOT NULL,
    errors         NVARCHAR(MAX)  NOT NULL,
    payload        NVARCHAR(MAX)  NOT NULL,
    detected_at    DATETIME2(3)   NOT NULL,
    resolved_at    DATETIME2(3)   NULL,
    resolved_by    NVARCHAR(128)  NULL,
    resolution     NVARCHAR(MAX)  NULL
);


-- Duplicate rows found inside a single vendor file. Under the dedup rule they
-- all survive as occurrences 1, 2, 3 of one identity, so nothing is lost -
-- but a vendor sending the same record twice is worth a person's attention,
-- so each group is recorded as a finding to raise.
--   key_values    the identity-column values the rows share
--   line_numbers  every line in the file that carried that identity
--   identical     1 if the rows were byte-identical (a pure re-send), 0 if
--                 they differed (two versions of one record in one file,
--                 which is the case that most needs a person)
IF OBJECT_ID('stg.duplicate_finding') IS NULL
CREATE TABLE stg.duplicate_finding (
    finding_id     BIGINT IDENTITY(1,1) PRIMARY KEY,
    tenant_id      NVARCHAR(64)   NOT NULL,
    batch_id       NVARCHAR(64)   NOT NULL,
    source_key     NVARCHAR(128)  NOT NULL,
    source_file_id BIGINT         NULL,
    key_values     NVARCHAR(900)  NOT NULL,
    line_numbers   NVARCHAR(400)  NOT NULL,
    identical      BIT            NOT NULL,
    detected_at    DATETIME2(3)   NOT NULL
);
"""

#: Columns added to live metadata tables after they were first created on
#: Azure. Each is in the CREATE above for a fresh database and here for an
#: existing one; see _add_column.
_META_COLUMNS_ADDED: tuple[tuple[str, str, str], ...] = (
    ("meta.source_file", "as_of", "DATE NULL"),
    ("meta.source_file", "as_of_source", "NVARCHAR(16) NULL"),
    ("meta.source_file", "coverage_from", "DATE NULL"),
    ("meta.source_file", "coverage_to", "DATE NULL"),
    ("meta.source_file", "header_json", "NVARCHAR(MAX) NULL"),
    ("meta.batch", "rows_landed", "INT NOT NULL DEFAULT 0"),
    ("meta.batch", "rows_late", "INT NOT NULL DEFAULT 0"),
    ("meta.batch", "rows_conflict", "INT NOT NULL DEFAULT 0"),
    ("meta.batch", "rows_vanished", "INT NOT NULL DEFAULT 0"),
)


#: A file's identity became (source, content hash, sheet) on 2026-10-22,
#: when the Cardinal summary got its own spec and two sources began to see
#: the same workbook. A database whose constraint predates that still has
#: it on (tenant, hash, sheet); this swaps it, once, and is a no-op after.
_SOURCE_FILE_IDENTITY_REPAIR = """
IF EXISTS (
    SELECT 1 FROM sys.key_constraints AS kc
    WHERE kc.name = 'uq_source_file' AND kc.parent_object_id = OBJECT_ID('meta.source_file')
    AND NOT EXISTS (
        SELECT 1 FROM sys.index_columns AS ic
        JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        WHERE ic.object_id = kc.parent_object_id AND ic.index_id = kc.unique_index_id
        AND c.name = 'source_key'))
BEGIN
    ALTER TABLE meta.source_file DROP CONSTRAINT uq_source_file
    ALTER TABLE meta.source_file ADD CONSTRAINT uq_source_file
        UNIQUE (tenant_id, source_key, file_sha256, sheet_name)
END
""".strip()


def meta_statements() -> list[str]:
    """Every statement needed for the metadata and staging schema.

    Returned as separate statements rather than one script: pyodbc executes one
    batch at a time, and a failure part way through a blob leaves a half-built
    schema with no clear way to tell what applied.

    Returns the six ``CREATE SCHEMA`` guards, the five tables, the guarded
    column additions for tables that were live before those columns existed,
    the file-identity constraint repair, and two indexes, in dependency order.
    """
    # Split the script on ';' and drop empty fragments. This is why no ';'
    # may appear inside a SQL comment in _META_TABLES.
    statements = [s.strip() for s in _META_TABLES.split(";") if s.strip()]
    statements += [_add_column(t, c, d) for t, c, d in _META_COLUMNS_ADDED]
    statements.append(_SOURCE_FILE_IDENTITY_REPAIR)
    # "What has source X delivered lately" - the freshness-alerting query.
    statements.append(
        _index("ix_batch_source", "meta.batch", "(tenant_id, source_key, started_at)")
    )
    # Filtered index over open quarantine only. Written out rather than via
    # _index because the WHERE clause belongs after the column list and the
    # helper's definition slot is a single string - same guard, same shape.
    statements.append(
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_quarantine_open' "
        "AND object_id = OBJECT_ID('stg.quarantine'))\n"
        "    CREATE INDEX ix_quarantine_open ON stg.quarantine "
        "(tenant_id, source_key, detected_at) WHERE resolved_at IS NULL"
    )
    return statements


#: Spec FieldType -> SQL column type for the typed and curated layers. This
#: is what makes "every destination column is typed from the typed layer
#: onward" (definition of done) true: a spec cannot declare a type that lands
#: as text there. The landed layer is NVARCHAR(MAX) throughout, by design.
#:   DECIMAL(38,6)  quantities, which can be fractional (dose units)
#:   DECIMAL(38,4)  money, four places so intermediate arithmetic in SQL does
#:                  not round cents
#:   The two scales are ``transforms.values.QUANTITY_PLACES`` and
#:   ``CURRENCY_PLACES``: the parser rounds to them, so a value never reaches
#:   the store with more places than its column holds (D15).
#:   CHAR(11)       NDC is always exactly 11 digits once normalized
#:   NVARCHAR(1000) text without a declared max_length; wide enough for the
#:                  longest observed free-text cell, narrow enough to index
_SQL_TYPES = {
    FieldType.TEXT: "NVARCHAR(1000)",
    FieldType.INTEGER: "BIGINT",
    FieldType.DECIMAL: f"DECIMAL(38,{QUANTITY_PLACES})",
    FieldType.CURRENCY: f"DECIMAL(38,{CURRENCY_PLACES})",
    FieldType.DATE: "DATE",
    FieldType.BOOLEAN: "BIT",
    FieldType.NDC: "CHAR(11)",
}


def table_name(spec: SourceSpec, layer: str = "curated") -> str:
    """``wholesaler.cardinal.invoice`` becomes ``curated.wholesaler_cardinal_invoice``.

    The same short name is used in every layer, so the three tables for one
    source differ only by schema and a (source_file_id, line_number) pair
    names the same physical line in all of them.
    """
    if layer not in LAYERS:
        raise ValueError(f"unknown layer {layer!r}; expected one of {LAYERS}")
    return f"{layer}." + spec.key.replace(".", "_")


def _all_targets(spec: SourceSpec) -> list[tuple[str, FieldType, int | None]]:
    """Every landed column across every loading variant, widest type wins.

    Returns ``(target, type, max_length)`` triples in a stable order: the
    widest variant's column order first, then anything only a narrower
    variant has. Stable order matters because the DDL is diffed in review.
    """
    seen: dict[str, tuple[FieldType, int | None]] = {}
    for variant in spec.variants:
        if not variant.loads:
            continue
        for column in variant.columns:
            if column.pii is PiiClass.PROHIBITED:
                # A prohibited column (SSN) gets no destination in any layer.
                # There is nowhere for the value to go even by accident.
                continue
            existing = seen.get(column.target)
            if existing is None:
                seen[column.target] = (column.type, column.max_length)
            elif existing[0] is not column.type:
                # Two variants disagree on a column's type. Text is the only
                # safe common ground, and it is worth knowing about.
                seen[column.target] = (FieldType.TEXT, None)
    # The spec owns the order (SourceSpec.landed_targets), and the row hash
    # runs over the same list, so the DDL and the hash agree on what a row is.
    return [(t, seen[t][0], seen[t][1]) for t in spec.landed_targets]


def typed_sql_types(spec: SourceSpec) -> list[str]:
    """The SQL type of every business column of ``typed`` and ``curated``, in
    ``spec.landed_targets`` order: the same tokens the DDL carries, so the
    commit engine can declare its parameters exactly as the columns are."""
    types = []
    for _target, kind, max_length in _all_targets(spec):
        sql_type = _SQL_TYPES[kind]
        if kind is FieldType.TEXT and max_length:
            # A declared max_length narrows the text column; max(…, 1) guards
            # against a zero that would be invalid DDL.
            sql_type = f"NVARCHAR({max(max_length, 1)})"
        types.append(sql_type)
    return types


def _typed_column_lines(spec: SourceSpec) -> list[str]:
    """One typed, nullable column definition per target, for typed and curated.

    Every business column is NULL-able because requiredness is enforced at
    typing time (a required blank quarantines) and a row that reached the
    table passed. Square brackets quote the name, so a target that is also a
    T-SQL keyword (e.g. "transaction") is still valid.
    """
    return [
        f"    [{target}] {sql_type} NULL,"
        for (target, _kind, _max), sql_type in zip(
            _all_targets(spec), typed_sql_types(spec), strict=True
        )
    ]


def _create(name: str, body: list[str]) -> str:
    """A guarded CREATE TABLE around the given column and constraint lines."""
    return "\n".join([f"IF OBJECT_ID('{name}') IS NULL", f"CREATE TABLE {name} (", *body, ")"])


def landed_statements(spec: SourceSpec) -> list[str]:
    """The landed table for one source: every cell as delivered text.

    One row per file line, including the footer and blank lines the typer
    will skip, keyed on (source_file_id, line_number). Columns are named by
    spec target, since the header-to-target binding is deterministic from the
    header row and the vendor's spelling is kept on
    ``meta.source_file.header_json``. ``NVARCHAR(MAX)`` throughout: a landed
    write must never fail on width, because this is the layer that proves
    what the vendor sent.

      created_at / created_by  which load wrote the row; the updated pair
                   is always null, because this layer is never changed
      extra_cells  JSON of header -> value for any delivered column no spec
                   column claimed (on_new_column: warn), so a column the
                   vendor added is kept from the day it appeared

    Prohibited columns are absent, as in every layer.
    """
    name = table_name(spec, "landed")
    short = spec.key.replace(".", "_")
    body = [
        "    tenant_id        NVARCHAR(64)   NOT NULL,",
        "    source_file_id   BIGINT         NOT NULL,",
        "    line_number      INT            NOT NULL,",
        *_IMMUTABLE_AUDIT_COLUMNS,
    ]
    body += [f"    [{target}] NVARCHAR(MAX) NULL," for target, _kind, _max in _all_targets(spec)]
    body += [
        "    extra_cells      NVARCHAR(MAX)  NULL,",
        f"    CONSTRAINT pk_landed_{short} PRIMARY KEY (source_file_id, line_number)",
    ]
    return [_create(name, body)]


def typed_statements(spec: SourceSpec) -> list[str]:
    """The typed table for one source: the landed row parsed per the spec.

    Keyed on (source_file_id, line_number) like landed, so the two join
    without a lookup. A line that fails typing has no row here and one in
    ``stg.quarantine``; landed = typed + quarantine (+ skipped footer and
    blank lines) is the per-file check.

      spec_sha256  SHA-256 of the spec file that typed the row. Typing rules
                   live in the spec, so this names the rule version exactly
                   and a re-type after a spec change is visible row by row
      row_hash     SHA-256 over the spec's row_hash_columns (core.identity)
      created_at / created_by  which load wrote the row; the updated pair
                   is always null, because this layer is rebuilt, never edited
    """
    name = table_name(spec, "typed")
    short = spec.key.replace(".", "_")
    body = [
        "    tenant_id        NVARCHAR(64)   NOT NULL,",
        "    source_file_id   BIGINT         NOT NULL,",
        "    line_number      INT            NOT NULL,",
        "    spec_sha256      CHAR(64)       NOT NULL,",
        "    row_hash         CHAR(64)       NOT NULL,",
        *_IMMUTABLE_AUDIT_COLUMNS,
    ]
    body += _typed_column_lines(spec)
    body.append(f"    CONSTRAINT pk_typed_{short} PRIMARY KEY (source_file_id, line_number)")
    return [
        _create(name, body),
        # "Has this exact content been typed before" - the re-delivery check.
        _index(f"ix_typed_{short}_hash", name, "(tenant_id, row_hash)"),
    ]


def curated_statements(spec: SourceSpec) -> list[str]:
    """The curated table for one source: the ledger over typed rows.

    Carries the business columns again rather than joining to typed, so that
    the views users query, and the supersession rules that need event dates,
    read one table. Storage is the accepted cost (architecture doc, "Storage
    layers").

    Returns the guarded CREATE TABLE and three indexes: live rows, identity
    lookup, and (when the spec declares one) the natural key for people's
    queries. The identity index is where the dedup rule runs: find the live
    row with this identity hash and occurrence, compare hash and as_of,
    decide the outcome - all without touching the table.
    """
    name = table_name(spec, "curated")
    short = spec.key.replace(".", "_")
    body = [CURATED_LEDGER_COLUMNS + ","]
    body += _typed_column_lines(spec)
    body.append(
        f"    CONSTRAINT pk_curated_{short} PRIMARY KEY (batch_id, source_file_id, line_number)"
    )
    statements = [
        _create(name, body),
        # The index every read uses: live rows for one tenant. INCLUDE lets
        # "what is current" answer from the index alone.
        _index(
            f"ix_curated_{short}_live",
            name,
            "(tenant_id, superseded_by) INCLUDE (identity_hash, occurrence, row_hash, as_of)",
        ),
        # The dedup lookup. Deliberately not unique: a superseded row keeps
        # its identity, so versions of one business row coexist by design;
        # uniqueness holds only among live rows, and the dedup logic rather
        # than the database enforces that.
        _index(
            f"ix_curated_{short}_identity",
            name,
            "(tenant_id, identity_hash, occurrence) INCLUDE (row_hash, superseded_by, as_of)",
        ),
    ]
    if spec.natural_key:
        key_cols = ", ".join(f"[{k}]" for k in spec.natural_key)
        # For people and views: "show me invoice 12345". The hash index above
        # serves the engine; this one serves the question asked in words.
        statements.append(_index(f"ix_curated_{short}_key", name, f"(tenant_id, {key_cols})"))
    if spec.coverage_target is not None:
        # The vanish outcome asks "which live rows fall inside the window this
        # delivery covers"; the coverage column (the event date, unless the
        # vendor filters on another) is the range predicate, and the INCLUDE
        # lets the identity comparison run from the index alone.
        statements.append(
            _index(
                f"ix_curated_{short}_coverage",
                name,
                f"(tenant_id, [{spec.coverage_target}]) "
                "INCLUDE (identity_hash, occurrence, superseded_by, as_of)",
            )
        )
    return statements


def table_statements(spec: SourceSpec) -> list[str]:
    """Every statement for one source across the three generated layers.

    The spec is the schema definition; there is no second place to keep the two
    in step. A column added to a spec becomes a migration, not a manual DDL edit
    that somebody forgets to make.

    Returned as separate statements because pyodbc executes one batch at a time,
    and a failure part way through a single blob leaves a half-built schema with
    no clear way to tell what applied. Order is landed, typed, curated.
    """
    return landed_statements(spec) + typed_statements(spec) + curated_statements(spec)
