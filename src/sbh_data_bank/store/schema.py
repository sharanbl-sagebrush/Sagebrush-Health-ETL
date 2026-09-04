"""The ledger schema, and the DDL generated from source specs.

Every fact table is append-only and bitemporal. Nothing is ever updated in
place and nothing is ever deleted. A correction is a new row plus a marker on
the old one, so the question "what did we believe on 1 August" stays a lookup
rather than a reconstruction - and the delete-by-date-range pattern in the
previous backfill scripts, which could remove rows belonging to other files,
becomes impossible to express.

Six columns carry that, on every fact table:

``tenant_id``       who owns the row. Non-null everywhere, from the first
                    migration, so a second covered entity is a configuration
                    change rather than a schema change.
``batch_id``        the load that wrote it.
``ingested_at``     transaction time - when we learned it.
``superseded_by``   the batch that replaced it, or NULL while it is current.
``superseded_at``   when that happened.
``row_hash``        content identity, over the spec's landed columns.

Current state is ``superseded_by IS NULL``. State as of a past moment is
``ingested_at <= @as_of AND (superseded_at IS NULL OR superseded_at > @as_of)``.

DUPLICATE HANDLING lives in :class:`DedupMode`, declared per source, because
the nineteen sources genuinely need three different answers and pretending
otherwise is how the previous pipeline came to MERGE four tables on keys that
were not unique.
"""

from __future__ import annotations

from enum import StrEnum

from sbh_data_bank.registry.spec import FieldType, PiiClass, SchemaVariant, SourceSpec

__all__ = [
    "DedupMode",
    "LEDGER_COLUMNS",
    "dedup_mode_for",
    "meta_statements",
    "table_statements",
    "table_name",
]


def _index(name: str, table: str, definition: str) -> str:
    """A guarded CREATE INDEX.

    T-SQL has no ``CREATE INDEX IF NOT EXISTS``; that is Postgres. Re-running a
    migration must be safe, so every index is guarded on sys.indexes.
    """
    return (
        f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = '{name}' "
        f"AND object_id = OBJECT_ID('{table}'))\n"
        f"    CREATE INDEX {name} ON {table} {definition}"
    )


class DedupMode(StrEnum):
    """How a source decides whether an incoming row is one it already has."""

    NATURAL_KEY = "natural_key"
    """Match on the declared business key.

    A key match with an identical row hash is a re-delivery and is skipped. A
    key match with a different hash is a correction: the new row is inserted and
    the old one superseded, so both survive and the change is visible. Fourteen
    of the nineteen sources use this.
    """

    ROW_HASH = "row_hash"
    """No business key exists, so the whole row is the identity.

    Correct for the denormalised report extracts - Athena Encounters, Athena
    Prescriptions, both Curve reports - where the vendor never intended a key
    and no subset of columns identifies a row. Verified safe for each: none of
    them contains byte-identical rows, so nothing real is collapsed.
    """

    FILE_POSITION = "file_position"
    """Identity is (file, line number). Nothing within a file is ever collapsed.

    For Allscripts eRx alone, and only because it needs it: 8,100 of its 132,974
    rows are identical across all 21 columns. Row-hash dedup would silently
    merge 8,100 real prescriptions into far fewer, so the file is taken as
    delivered and duplicate *files* are prevented by the SHA-256 file check.

    The cost is that two overlapping deliveries both land in full. That is a
    known limitation, not a solved problem, and the fix is either an order
    identifier from Allscripts or snapshot semantics per delivery.
    """


#: Sources that cannot use their row hash as identity.
_FILE_POSITION_SOURCES = frozenset({"ehr.allscripts.erx"})


def dedup_mode_for(spec: SourceSpec) -> DedupMode:
    """The duplicate policy for a source, derived from its spec.

    Derived rather than declared separately, so it cannot drift from the
    natural key it depends on.
    """
    if spec.key in _FILE_POSITION_SOURCES:
        return DedupMode.FILE_POSITION
    if spec.natural_key:
        return DedupMode.NATURAL_KEY
    return DedupMode.ROW_HASH


LEDGER_COLUMNS = """    tenant_id        NVARCHAR(64)   NOT NULL,
    batch_id         NVARCHAR(64)   NOT NULL,
    source_file_id   BIGINT         NOT NULL,
    line_number      INT            NOT NULL,
    row_hash         CHAR(64)       NOT NULL,
    ingested_at      DATETIME2(3)   NOT NULL,
    superseded_by    NVARCHAR(64)   NULL,
    superseded_at    DATETIME2(3)   NULL"""


_META_TABLES = """
IF SCHEMA_ID('meta') IS NULL EXEC('CREATE SCHEMA meta');
IF SCHEMA_ID('raw')  IS NULL EXEC('CREATE SCHEMA raw');
IF SCHEMA_ID('stg')  IS NULL EXEC('CREATE SCHEMA stg');

-- One row per physical file ever seen. Identity is the content hash, so the
-- same extract delivered under a different name is recognised as already
-- processed, and a corrected file under the same name is not.
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
    CONSTRAINT uq_source_file UNIQUE (tenant_id, file_sha256, sheet_name)
);

-- One row per attempt to load one file (or one worksheet of one file).
IF OBJECT_ID('meta.batch') IS NULL
CREATE TABLE meta.batch (
    batch_id          NVARCHAR(64)  NOT NULL PRIMARY KEY,
    tenant_id         NVARCHAR(64)  NOT NULL,
    run_id            NVARCHAR(64)  NOT NULL,
    source_key        NVARCHAR(128) NOT NULL,
    source_file_id    BIGINT        NULL,
    variant_name      NVARCHAR(128) NULL,
    dedup_mode        NVARCHAR(32)  NULL,
    outcome           NVARCHAR(40)  NOT NULL,
    detail            NVARCHAR(MAX) NULL,
    rows_read         INT           NOT NULL DEFAULT 0,
    rows_inserted     INT           NOT NULL DEFAULT 0,
    rows_superseded   INT           NOT NULL DEFAULT 0,
    rows_unchanged    INT           NOT NULL DEFAULT 0,
    rows_quarantined  INT           NOT NULL DEFAULT 0,
    control_totals    NVARCHAR(MAX) NULL,
    started_at        DATETIME2(3)  NOT NULL,
    finished_at       DATETIME2(3)  NULL
);


-- One row per pipeline execution.
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


-- Duplicate rows found inside a single vendor file. Collapsing them silently
-- makes the totals right and hides that a vendor is sending the same record
-- twice, so they are recorded as findings for somebody to raise.
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


def meta_statements() -> list[str]:
    """Every statement needed for the metadata and staging schema.

    Returned as separate statements rather than one script: pyodbc executes one
    batch at a time, and a failure part way through a blob leaves a half-built
    schema with no clear way to tell what applied.
    """
    statements = [s.strip() for s in _META_TABLES.split(";") if s.strip()]
    statements.append(
        _index("ix_batch_source", "meta.batch", "(tenant_id, source_key, started_at)")
    )
    statements.append(
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_quarantine_open' "
        "AND object_id = OBJECT_ID('stg.quarantine'))\n"
        "    CREATE INDEX ix_quarantine_open ON stg.quarantine "
        "(tenant_id, source_key, detected_at) WHERE resolved_at IS NULL"
    )
    return statements


_SQL_TYPES = {
    FieldType.TEXT: "NVARCHAR(1000)",
    FieldType.INTEGER: "BIGINT",
    FieldType.DECIMAL: "DECIMAL(38,6)",
    FieldType.CURRENCY: "DECIMAL(38,4)",
    FieldType.DATE: "DATE",
    FieldType.BOOLEAN: "BIT",
    FieldType.NDC: "CHAR(11)",
}


def table_name(spec: SourceSpec) -> str:
    """``wholesaler.cardinal.invoice`` becomes ``raw.wholesaler_cardinal_invoice``."""
    return "raw." + spec.key.replace(".", "_")


def _widest_variant(spec: SourceSpec) -> SchemaVariant:
    """The variant with the most columns.

    One table serves every variant of a source, so it must be wide enough for
    all of them - a WeInfuse row from the legacy export and one from the new
    reporting module land in the same table under the same canonical names.
    """
    return max((v for v in spec.variants if v.loads), key=lambda v: len(v.columns))


def _all_targets(spec: SourceSpec) -> list[tuple[str, FieldType, int | None]]:
    """Every landed column across every loading variant, widest type wins."""
    seen: dict[str, tuple[FieldType, int | None]] = {}
    for variant in spec.variants:
        if not variant.loads:
            continue
        for column in variant.columns:
            if column.pii is PiiClass.PROHIBITED:
                continue
            existing = seen.get(column.target)
            if existing is None:
                seen[column.target] = (column.type, column.max_length)
            elif existing[0] is not column.type:
                # Two variants disagree on a column's type. Text is the only
                # safe common ground, and it is worth knowing about.
                seen[column.target] = (FieldType.TEXT, None)
    order = [c.target for c in _widest_variant(spec).columns if c.target in seen]
    order += [t for t in seen if t not in order]
    return [(t, seen[t][0], seen[t][1]) for t in order]


def table_statements(spec: SourceSpec) -> list[str]:
    """CREATE TABLE plus indexes for one source, generated from its spec.

    The spec is the schema definition; there is no second place to keep the two
    in step. A column added to a spec becomes a migration, not a manual DDL edit
    that somebody forgets to make.

    Returned as separate statements because pyodbc executes one batch at a time,
    and a failure part way through a single blob leaves a half-built schema with
    no clear way to tell what applied.
    """
    name = table_name(spec)
    short = spec.key.replace(".", "_")

    lines = [f"IF OBJECT_ID('{name}') IS NULL", f"CREATE TABLE {name} (", LEDGER_COLUMNS + ","]
    for target, kind, max_length in _all_targets(spec):
        sql_type = _SQL_TYPES[kind]
        if kind is FieldType.TEXT and max_length:
            sql_type = f"NVARCHAR({max(max_length, 1)})"
        lines.append(f"    [{target}] {sql_type} NULL,")
    lines.append(f"    CONSTRAINT pk_{short} PRIMARY KEY (batch_id, source_file_id, line_number)")
    lines.append(")")

    statements = ["\n".join(lines)]

    # The index every read uses: live rows for one tenant.
    statements.append(
        _index(f"ix_{short}_live", name, "(tenant_id, superseded_by) INCLUDE (row_hash)")
    )

    if spec.natural_key:
        key_cols = ", ".join(f"[{k}]" for k in spec.natural_key)
        # Deliberately not a unique index. A superseded row keeps its key, so
        # several versions of one business row coexist by design; uniqueness
        # holds only among live rows, and that is enforced by the dedup logic
        # rather than by the database.
        statements.append(
            _index(
                f"ix_{short}_key",
                name,
                f"(tenant_id, {key_cols}) INCLUDE (row_hash, superseded_by)",
            )
        )
    else:
        statements.append(
            _index(f"ix_{short}_hash", name, "(tenant_id, row_hash) INCLUDE (superseded_by)")
        )
    return statements
