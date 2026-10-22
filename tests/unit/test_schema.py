"""The DDL generated from the shipped specs.

``store.schema`` turns every spec into three tables (landed, typed, curated)
plus the metadata tables. These tests hold the properties the layered design
depends on, against the real specs in ``specs/``:

- every statement is guarded, so the migration can be re-run;
- the landed layer is text throughout and the typed layer is typed throughout;
- a prohibited column (SSN) has no destination in any layer;
- the three tables of one source share the (source_file_id, line_number) key;
- the curated ledger carries the dedup rule's columns and indexes;
- identity columns follow the key when there is one and every column when not.

No database is needed; the module produces text and these tests read it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sbh_data_bank.registry.loader import SpecRegistry
from sbh_data_bank.registry.spec import PiiClass
from sbh_data_bank.store.schema import (
    DEDUP_RULE,
    LAYERS,
    LEDGER_COLUMN_NAMES,
    curated_statements,
    identity_columns_for,
    landed_statements,
    meta_statements,
    table_name,
    table_statements,
    typed_statements,
)

SPEC_DIR = Path(__file__).resolve().parents[2] / "specs"


@pytest.fixture(scope="module")
def registry() -> SpecRegistry:
    return SpecRegistry.from_dir(SPEC_DIR)


def _columns(create_statement: str) -> dict[str, str]:
    """Column name -> type text, from one CREATE TABLE statement."""
    found = {}
    for line in create_statement.splitlines():
        m = re.match(r"\s+\[?([A-Za-z_][A-Za-z0-9_]*)\]?\s+([A-Z][A-Z0-9(),]*)", line)
        if m and not line.strip().startswith("CONSTRAINT"):
            found[m.group(1)] = m.group(2)
    return found


def _sql(statement: str) -> str:
    """The statement without the leading SQL comment block that documents it."""
    lines = statement.splitlines()
    while lines and lines[0].startswith("--"):
        lines.pop(0)
    return chr(10).join(lines)


class TestMetaStatements:
    def test_every_statement_is_guarded(self):
        for statement in meta_statements():
            assert _sql(statement).startswith("IF "), statement[:60]

    def test_a_file_is_identified_by_its_source_as_well_as_its_hash(self):
        """Two specs can see one workbook (Cardinal summary and detail), so
        the unique constraint carries the source key, and a database built
        before 2026-10-22 gets its constraint swapped, once."""
        text = "\n".join(meta_statements())
        assert "UNIQUE (tenant_id, source_key, file_sha256, sheet_name)" in text
        repair = next(s for s in meta_statements() if "DROP CONSTRAINT uq_source_file" in s)
        assert "c.name = 'source_key'" in repair and repair.startswith("IF EXISTS")

    def test_all_six_schemas_are_created(self):
        text = "\n".join(meta_statements())
        for schema in ("meta", "stg", *LAYERS, "pres"):
            assert f"CREATE SCHEMA {schema}" in text

    def test_no_semicolon_inside_a_comment_broke_the_split(self):
        # Each fragment must be one statement: a comment that contained ';'
        # would produce a fragment with no SQL verb in it.
        for statement in meta_statements():
            assert re.search(r"\b(CREATE|ALTER|EXEC)\b", statement), statement[:80]

    def test_live_tables_get_their_new_columns_by_guarded_alter(self):
        text = "\n".join(meta_statements())
        for table, column in (
            ("meta.source_file", "as_of"),
            ("meta.source_file", "as_of_source"),
            ("meta.source_file", "header_json"),
            ("meta.batch", "rows_late"),
            ("meta.batch", "rows_conflict"),
            ("meta.batch", "rows_vanished"),
        ):
            assert f"IF COL_LENGTH('{table}', '{column}') IS NULL" in text
            # and the same column is in the CREATE for a fresh database
            create = next(s for s in meta_statements() if f"CREATE TABLE {table}" in s)
            assert column in _columns(create)


class TestPerSourceStatements:
    def test_three_layers_per_source_in_order(self, registry):
        for spec in registry:
            statements = table_statements(spec)
            creates = [s for s in statements if "CREATE TABLE" in s]
            assert [s.splitlines()[1].split()[2] for s in creates] == [
                table_name(spec, layer) for layer in LAYERS
            ]

    def test_every_statement_is_guarded(self, registry):
        for spec in registry:
            for statement in table_statements(spec):
                assert statement.startswith("IF "), statement[:60]

    def test_landed_is_text_throughout(self, registry):
        for spec in registry:
            (create,) = landed_statements(spec)
            business = {k: v for k, v in _columns(create).items() if k not in LEDGER_COLUMN_NAMES}
            assert business, spec.key
            assert set(business.values()) == {"NVARCHAR(MAX)"}, spec.key

    def test_typed_and_curated_carry_no_text_catch_all(self, registry):
        for spec in registry:
            for create in (typed_statements(spec)[0], curated_statements(spec)[0]):
                for column, sql_type in _columns(create).items():
                    if column in {"extra_cells", "header_json"}:
                        continue
                    assert sql_type != "NVARCHAR(MAX)", (spec.key, column)

    def test_a_declared_max_length_sets_the_width_and_widens_a_live_table(self, registry):
        """WeInfuse patient notes: NVARCHAR(4000) in the create, and a
        guarded ALTER COLUMN for a table built before the width was
        declared (the first patients load failed on a 2,620-character
        note). Widening is the one alteration the migration makes."""
        spec = registry.require("dispense.weinfuse.patients")
        for statements, table in (
            (typed_statements(spec), "typed.dispense_weinfuse_patients"),
            (curated_statements(spec), "curated.dispense_weinfuse_patients"),
        ):
            assert _columns(statements[0])["notes"] == "NVARCHAR(4000)"
            (widen,) = [s for s in statements if "ALTER COLUMN" in s]
            assert widen.startswith("IF EXISTS (SELECT 1 FROM sys.columns")
            assert f"ALTER TABLE {table} ALTER COLUMN [notes] NVARCHAR(4000) NULL" in widen
            assert "max_length BETWEEN 1 AND 7999" in widen
        # The default width is the parser's limit, declared once.
        from sbh_data_bank.transforms.values import TEXT_LENGTH

        assert _columns(typed_statements(spec)[0])["first_name"] == f"NVARCHAR({TEXT_LENGTH})"

    def test_the_three_tables_share_the_file_line_key(self, registry):
        for spec in registry:
            for statement in table_statements(spec):
                if "CREATE TABLE" in statement:
                    assert "PRIMARY KEY (" in statement
                    assert "source_file_id, line_number)" in statement, spec.key

    def test_business_columns_are_the_same_in_every_layer(self, registry):
        ledger = LEDGER_COLUMN_NAMES
        for spec in registry:
            per_layer = []
            for statements in (landed_statements, typed_statements, curated_statements):
                create = statements(spec)[0]
                per_layer.append(set(_columns(create)) - ledger)
            assert per_layer[0] == per_layer[1] == per_layer[2], spec.key

    def test_prohibited_columns_have_no_destination_in_any_layer(self, registry):
        prohibited = {
            (spec.key, column.target)
            for spec in registry
            for variant in spec.variants
            for column in variant.columns
            if column.pii is PiiClass.PROHIBITED
        }
        assert prohibited, "the specs are expected to declare at least one prohibited column"
        for spec in registry:
            text = "\n".join(table_statements(spec))
            for key, target in prohibited:
                if key == spec.key:
                    assert f"[{target}]" not in text, (spec.key, target)

    def test_curated_carries_the_dedup_rule_columns_and_indexes(self, registry):
        for spec in registry:
            statements = curated_statements(spec)
            columns = _columns(statements[0])
            for needed in ("dedup_rule", "identity_hash", "occurrence", "as_of", "outcome"):
                assert needed in columns, (spec.key, needed)
            text = "\n".join(statements)
            assert "_live ON " in text and "_identity ON " in text
            assert ("_key ON " in text) == bool(spec.natural_key), spec.key

    def test_no_generated_table_repeats_a_column_name(self, registry):
        """The first Azure migration failed here: a vendor column mapped to
        ``created_at`` collided with the audit column of the same name
        (2026-10-21). The spec model now refuses the name; this sweep is the
        backstop for any future ledger column."""
        for spec in registry:
            for statement in table_statements(spec):
                if "CREATE TABLE" not in statement:
                    continue
                names = [
                    m.group(1)
                    for line in statement.splitlines()
                    if (m := re.match(r"\s+\[?([A-Za-z_][A-Za-z0-9_]*)\]?\s+[A-Z]", line))
                    and not line.strip().startswith("CONSTRAINT")
                ]
                repeated = sorted({n for n in names if names.count(n) > 1})
                assert not repeated, f"{spec.key}: repeated columns {repeated}"

    def test_every_layer_carries_the_four_audit_columns(self, registry):
        """Decision D11: created_at/by on every table, updated_at/by null on
        the immutable layers and set only on supersession in curated."""
        for spec in registry:
            for statements in (landed_statements, typed_statements, curated_statements):
                columns = _columns(statements(spec)[0])
                for name in ("created_at", "created_by", "updated_at", "updated_by"):
                    assert name in columns, (spec.key, name)
                for stale in ("landed_at", "typed_at", "ingested_at"):
                    assert stale not in columns, (spec.key, stale)

    def test_a_declared_event_date_gets_a_coverage_index(self, registry):
        """The vanish outcome's range predicate. Only sources that declare an
        event date can vanish, so only they carry the index."""
        for spec in registry:
            text = chr(10).join(curated_statements(spec))
            assert ("_coverage ON " in text) == (spec.coverage_target is not None), spec.key
            if spec.coverage_target is not None:
                assert f"(tenant_id, [{spec.coverage_target}])" in text

    def test_identity_columns_follow_the_key_or_every_column(self, registry):
        for spec in registry:
            identity = identity_columns_for(spec)
            if spec.natural_key:
                assert identity == tuple(spec.natural_key)
            else:
                (create,) = landed_statements(spec)
                business = set(_columns(create)) - LEDGER_COLUMN_NAMES
                assert set(identity) == business, spec.key

    def test_ddl_is_deterministic(self, registry):
        for spec in registry:
            assert table_statements(spec) == table_statements(spec)

    def test_table_name_rejects_an_unknown_layer(self, registry):
        spec = next(iter(registry))
        with pytest.raises(ValueError):
            table_name(spec, "raw")


def test_dedup_rule_is_versioned():
    assert re.fullmatch(r"[a-z_]+\.v\d+", DEDUP_RULE)
