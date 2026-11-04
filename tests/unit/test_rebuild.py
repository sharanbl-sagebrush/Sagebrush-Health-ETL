"""The write side of the replay (``commit.rebuild``, decision D18).

Each test loads real files through the engine into the in-memory store,
edits the spec, and rebuilds. What is held: a rebuild under an unchanged
spec touches no curated row; a key change retires every live row as
``retyped`` and inserts its replacement with the same business values,
leaving exactly one current version per identity; a hash change does the
same for the rows it affects and keeps the rest; a column typed
differently is altered in the live tables and refilled from the replay on
every curated row, history included; a spec edit that would quarantine a
typed line is refused with nothing written; and a plan (``apply=False``)
reports the same counts as the write.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sbh_data_bank.commit.engine import CommitEngine
from sbh_data_bank.commit.memory import MemoryStore
from sbh_data_bank.commit.rebuild import REBUILT, RETYPED, rebuild_source
from sbh_data_bank.commit.sql import (
    alter_sql,
    live_type_token,
    null_sql,
    orphan_sql,
    rebuilt_insert_sql,
    refill_sql,
    retire_sql,
    typed_delete_sql,
)
from sbh_data_bank.registry.spec import SourceSpec

CLOCK = datetime(2026, 11, 4, 9, 0, 0)
LATER = datetime(2026, 11, 4, 10, 0, 0)
HEADER = "ID,Account,Event,Amount,Note"


def _spec(**overrides) -> SourceSpec:
    base = {
        "key": "t.vendor.report",
        "name": "n",
        "vendor": "v",
        "natural_key": ["id"],
        "event_date": "event",
        "coverage": "filename",
        "max_quarantine_rate": 0.5,
        "variants": [
            {
                "name": "only",
                "detect": {"require_all": ["ID", "Event"]},
                "columns": [
                    {"target": "id", "sources": ["ID"], "required": True},
                    {"target": "account", "sources": ["Account"]},
                    {
                        "target": "event",
                        "sources": ["Event"],
                        "type": "date",
                        "date_formats": ["%Y-%m-%d"],
                        "required": True,
                    },
                    {"target": "amount", "sources": ["Amount"], "type": "text"},
                    {"target": "note", "sources": ["Note"]},
                ],
            }
        ],
    }
    return SourceSpec.model_validate({**base, **overrides})


def _with_columns(spec_kwargs: dict, **column_overrides) -> SourceSpec:
    """The base spec with one or more columns' fields replaced."""
    spec = _spec(**spec_kwargs)
    data = spec.model_dump(mode="json", exclude_none=True)
    for column in data["variants"][0]["columns"]:
        if column["target"] in column_overrides:
            column.update(column_overrides[column["target"]])
    return SourceSpec.model_validate(data)


def _write(folder: Path, name: str, *rows: str) -> Path:
    path = folder / name
    path.write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")
    return path


JAN = "t_01012026-01312026.csv"
FEB = "t_01012026-02282026.csv"
A1 = "A,X,2026-01-10,10.00,a"
A2 = "A,Y,2026-01-11,12.00,a"  # same id as A1, another account: occurrence 2
B = "B,X,2026-01-12,20.00,b"


def _loaded(tmp_path: Path, spec: SourceSpec) -> MemoryStore:
    store = MemoryStore()
    engine = CommitEngine(
        store, tenant_id="t1", actor="loader", clock=lambda: CLOCK, deterministic_ids=True
    )
    engine.commit_path(spec, _write(tmp_path, JAN, A1, A2, B), run_id="r", root=tmp_path)
    engine.commit_path(
        spec, _write(tmp_path, FEB, A1, A2, "B,X,2026-01-12,21.00,b"), run_id="r", root=tmp_path
    )
    return store


def _rebuild(store: MemoryStore, spec: SourceSpec, *, apply: bool):
    return rebuild_source(
        store, spec, tenant_id="t1", actor="rebuilder", run_id="r2", apply=apply,
        clock=lambda: LATER,
    )  # fmt: skip


def _ident(row) -> tuple[str, int, str]:
    return (row["identity_hash"], row["occurrence"], row["row_hash"])


def _live(store: MemoryStore, spec: SourceSpec):
    return sorted(store.live(spec.key), key=lambda r: (r["source_file_id"], r["line_number"]))


class TestUnchangedSpec:
    def test_nothing_in_curated_moves_and_typed_is_rewritten(self, tmp_path):
        spec = _spec()
        store = _loaded(tmp_path, spec)
        before = store.snapshot()
        live_before = [_ident(r) for r in _live(store, spec)]

        result = _rebuild(store, spec, apply=True)

        assert not result.refused, result.refusal
        assert (result.files, result.lines, result.rehashed) == (2, 6, 0)
        assert (result.counts.kept, result.counts.retired, result.counts.inserted) == (3, 0, 0)
        # Three identities current: (A,1), (A,2), (B,1); B's FEB correction superseded JAN's.
        assert [_ident(r) for r in _live(store, spec)] == live_before
        assert store.curated[spec.key] == before["curated"][spec.key]
        # Typed rows were replaced, under the rebuild's stamp, with the same hashes.
        assert {r["row_hash"] for r in store.typed[spec.key]} == {
            r["row_hash"] for r in before["typed"][spec.key]
        }
        assert all(r["created_by"] == "rebuilder" for r in store.typed[spec.key])
        batch = store.batches[result.batch_id]
        assert (batch.outcome, batch.rows_unchanged, batch.rows_superseded) == (REBUILT, 3, 0)
        assert batch.source_file_id is None

    def test_a_plan_writes_nothing(self, tmp_path):
        spec = _spec()
        store = _loaded(tmp_path, spec)
        before = store.snapshot()

        result = _rebuild(store, spec, apply=False)

        assert not result.refused and result.batch_id is None
        assert (result.counts.kept, result.counts.retired, result.counts.inserted) == (3, 0, 0)
        assert store.snapshot() == before


class TestKeyChange:
    def test_every_live_row_is_retired_and_replaced(self, tmp_path):
        """``account`` joins the key: A's two lines become two identities
        instead of two occurrences of one, and B's identity hash changes
        too. Every live row is retired as retyped; every replacement
        carries the same business values and the rebuild batch."""
        old = _spec()
        store = _loaded(tmp_path, old)
        live_before = _live(store, old)
        new = _spec(natural_key=["id", "account"])

        plan = _rebuild(store, new, apply=False)
        result = _rebuild(store, new, apply=True)

        assert plan.counts == result.counts
        assert (result.counts.kept, result.counts.retired, result.counts.inserted) == (0, 3, 3)
        retired = [r for r in store.curated[new.key] if r["supersede_reason"] == RETYPED]
        assert len(retired) == 3
        assert all(r["superseded_by"] == result.batch_id for r in retired)
        assert all(r["superseded_at"] == LATER for r in retired)
        live_after = _live(store, new)
        assert len(live_after) == 3
        assert all(r["batch_id"] == result.batch_id for r in live_after)
        assert all(r["occurrence"] == 1 for r in live_after)
        assert len({r["identity_hash"] for r in live_after}) == 3
        # Same lines are current, same values; only the identity changed.
        assert [(r["source_file_id"], r["line_number"]) for r in live_after] == [
            (r["source_file_id"], r["line_number"]) for r in live_before
        ]
        for before, after in zip(live_before, live_after, strict=True):
            assert before["row_hash"] == after["row_hash"]
            assert {c: before[c] for c in ("id", "account", "amount", "note")} == {
                c: after[c] for c in ("id", "account", "amount", "note")
            }
            assert before["identity_hash"] != after["identity_hash"]
        # History before the rebuild is untouched: JAN's B row stays corrected.
        corrected = [r for r in store.curated[new.key] if r["supersede_reason"] == "corrected"]
        assert len(corrected) == 1

    def test_one_current_version_per_identity(self, tmp_path):
        store = _loaded(tmp_path, _spec())
        new = _spec(natural_key=["id", "account"])
        _rebuild(store, new, apply=True)
        keys = [(r["identity_hash"], r["occurrence"]) for r in store.live(new.key)]
        assert len(keys) == len(set(keys))


class TestHashChange:
    def test_only_the_rows_whose_typing_changed_move(self, tmp_path):
        """``note`` leaves the row hash: every row's hash changes, so every
        live row is retired. ``amount`` typed as currency instead of text
        would do the same. Here the exclusion is used because it changes
        hashes without changing values."""
        store = _loaded(tmp_path, _spec())
        new = _spec(row_hash_exclude=["note"])

        result = _rebuild(store, new, apply=True)

        assert not result.refused
        assert result.rehashed == 6
        assert (result.counts.kept, result.counts.retired, result.counts.inserted) == (0, 3, 3)
        assert all(r["superseded_by"] == result.batch_id for r in store.curated[new.key]
                   if r["supersede_reason"] == RETYPED)  # fmt: skip
        assert {r["spec_sha256"] for r in store.typed[new.key]} == {result.spec_sha256}

    def test_a_change_that_touches_no_row_keeps_everything(self, tmp_path):
        """A stricter detector changes the digest but not one hash: typed is
        rewritten under the new digest, curated is kept as it was. (A new
        column, even one no file carries, is in every row hash, so that
        would be a rehash of every row, not this case.)"""
        store = _loaded(tmp_path, _spec())
        data = _spec().model_dump(mode="json", exclude_none=True)
        data["variants"][0]["detect"] = {"require_all": ["ID", "Event", "Account"]}
        new = SourceSpec.model_validate(data)
        before = store.snapshot()["curated"][new.key]

        result = _rebuild(store, new, apply=True)

        assert not result.refused and result.rehashed == 0
        assert (result.counts.kept, result.counts.retired, result.counts.inserted) == (3, 0, 0)
        assert store.curated[new.key] == before
        assert {r["spec_sha256"] for r in store.typed[new.key]} == {result.spec_sha256}


class TestTypeChange:
    def test_the_live_columns_are_altered_and_every_curated_row_refilled(self, tmp_path):
        """``amount`` becomes currency. The live column is text on both
        tables (recorded at the first write, as the migration made it); the
        rebuild alters it and refills the four curated rows, the superseded
        January B included, with the replay's Decimal, while the plan
        reports the same and touches nothing."""
        store = _loaded(tmp_path, _spec())
        new = _with_columns({}, amount={"type": "currency"})
        assert all(isinstance(r["amount"], str) for r in store.curated[new.key])

        plan = _rebuild(store, new, apply=False)

        assert not plan.refused
        assert [(r.target, r.live_type, r.spec_type) for r in plan.retypes] == [
            ("amount", "NVARCHAR(1000)", "DECIMAL(38,4)")
        ]
        assert plan.refilled == 4
        assert store.column_types[new.key]["amount"] == "NVARCHAR(1000)"
        assert all(isinstance(r["amount"], str) for r in store.curated[new.key])

        result = _rebuild(store, new, apply=True)

        assert not result.refused and result.rehashed == 6
        assert (result.counts.kept, result.counts.retired, result.counts.inserted) == (0, 3, 3)
        assert result.refilled == 4
        assert store.column_types[new.key]["amount"] == "DECIMAL(38,4)"
        assert all(isinstance(r["amount"], Decimal) for r in store.curated[new.key])
        assert all(isinstance(r["amount"], Decimal) for r in store.typed[new.key])
        history = [r for r in store.curated[new.key] if r["supersede_reason"] == "corrected"]
        assert [r["amount"] for r in history] == [Decimal("20.00")]
        assert sorted(r["amount"] for r in _live(store, new)) == [
            Decimal("10.00"), Decimal("12.00"), Decimal("21.00"),
        ]  # fmt: skip
        assert "retyping amount NVARCHAR(1000)->DECIMAL(38,4)" in result.summary()
        assert "history refilled 4" in result.summary()

    def test_a_curated_row_with_no_typed_line_refuses_the_source(self, tmp_path):
        """The refill rests on every curated row having its line in typed.
        A stray row breaks that, and the rebuild refuses rather than leave
        it null under the new type."""
        store = _loaded(tmp_path, _spec())
        new = _with_columns({}, amount={"type": "currency"})
        stray = {**store.curated[new.key][0], "line_number": 999}
        store.curated[new.key].append(stray)
        before = store.snapshot()

        result = _rebuild(store, new, apply=True)

        assert result.refused and "no typed line" in result.refusal
        assert store.snapshot() == before

    def test_an_unchanged_type_retypes_nothing(self, tmp_path):
        store = _loaded(tmp_path, _spec())
        result = _rebuild(store, _spec(row_hash_exclude=["note"]), apply=True)
        assert not result.refused and result.retypes == [] and result.refilled == 0
        assert "retyping" not in result.summary()


class TestRefusals:
    def test_a_line_that_would_newly_quarantine_refuses_the_source(self, tmp_path):
        store = _loaded(tmp_path, _spec())
        before = store.snapshot()
        new = _with_columns({}, note={"type": "date", "date_formats": ["%Y-%m-%d"]})

        result = _rebuild(store, new, apply=True)

        assert result.refused
        assert "would newly quarantine" in result.refusal
        assert result.batch_id is None
        assert store.snapshot() == before

    def test_a_spec_that_no_longer_loads_the_file_refuses(self, tmp_path):
        store = _loaded(tmp_path, _spec())
        before = store.snapshot()
        new = _with_columns({}, note={"required": True, "sources": ["Missing"]})

        result = _rebuild(store, new, apply=True)

        assert result.refused and "does not load it" in result.refusal
        assert store.snapshot() == before


class TestStatements:
    def test_a_type_change_nulls_alters_and_refills_by_line(self):
        from sbh_data_bank.commit.rebuild import ColumnRetype

        spec = _spec()
        retype = ColumnRetype("amount", "NVARCHAR(1000)", "DECIMAL(38,4)")
        assert null_sql(spec, "typed", [retype]) == (
            "UPDATE typed.t_vendor_report SET [amount] = NULL WHERE tenant_id = ?"
        )
        assert alter_sql(spec, "curated", retype) == (
            "ALTER TABLE curated.t_vendor_report ALTER COLUMN [amount] DECIMAL(38,4) NULL"
        )
        line = (
            "ty.tenant_id = c.tenant_id AND ty.source_file_id = c.source_file_id "
            "AND ty.line_number = c.line_number"
        )
        assert refill_sql(spec, [retype], count_only=False) == (
            "UPDATE c SET c.[amount] = ty.[amount] FROM curated.t_vendor_report AS c "
            f"JOIN typed.t_vendor_report AS ty ON {line} WHERE c.tenant_id = ?"
        )
        assert refill_sql(spec, [retype], count_only=True).startswith("SELECT COUNT(*) FROM")
        assert orphan_sql(spec) == (
            "SELECT COUNT(*) FROM curated.t_vendor_report AS c WHERE c.tenant_id = ? "
            f"AND NOT EXISTS (SELECT 1 FROM typed.t_vendor_report AS ty WHERE {line})"
        )

    def test_a_catalog_row_is_spelled_as_the_ddl_spells_it(self):
        assert live_type_token("nvarchar", 2000, 0, 0) == "NVARCHAR(1000)"
        assert live_type_token("nvarchar", -1, 0, 0) == "NVARCHAR(MAX)"
        assert live_type_token("decimal", 17, 38, 4) == "DECIMAL(38,4)"
        assert live_type_token("char", 11, 0, 0) == "CHAR(11)"
        assert live_type_token("bigint", 8, 19, 0) == "BIGINT"
        assert live_type_token("date", 3, 10, 0) == "DATE"
        assert live_type_token("bit", 1, 1, 0) == "BIT"
        assert live_type_token("datetime2", 7, 23, 3) == "DATETIME2(3)"

    def test_the_sql_is_parameterised_and_keyed_on_the_match(self):
        spec = _spec()
        assert typed_delete_sql(spec) == (
            "DELETE FROM typed.t_vendor_report WHERE tenant_id = ? AND source_file_id = ?"
        )
        retire = retire_sql(spec, count_only=False)
        assert retire.startswith("UPDATE c SET superseded_by = ?, superseded_at = ?")
        assert "LEFT JOIN #target AS t ON" in retire and "t.row_hash = c.row_hash" in retire
        assert retire.endswith("AND t.source_file_id IS NULL")
        assert retire_sql(spec, count_only=True).startswith("SELECT COUNT(*) FROM curated.")
        insert = rebuilt_insert_sql(spec, count_only=False)
        assert insert.startswith("INSERT INTO curated.t_vendor_report ([tenant_id], [batch_id]")
        assert "ty.[id], ty.[account], ty.[event], ty.[amount], ty.[note]" in insert
        assert "JOIN typed.t_vendor_report AS ty" in insert
        assert "NOT EXISTS" in insert
        assert "'" not in insert and "'" not in retire
