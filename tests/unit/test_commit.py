"""The commit engine end to end, against the in-memory store.

One small keyed spec with an event date, coverage from the filename and a
vanish scope, and CSV files written to ``tmp_path`` whose names carry a date
window, so every delivery fact comes from the same chain the real files go
through. Each test is one line of the outcome table in CLAUDE.md
("Duplicates") or one of the engine's refusals, checked on what reached the
store: which rows are live, who superseded whom and why, and whether the
batch accounts for every row it read.

``TestSqlStore`` holds the T-SQL against the DDL ``store.schema`` generates
and runs one commit through a recording cursor, so the statement shapes are
tested without a server.
"""

from __future__ import annotations

import re
import zipfile
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sbh_data_bank.commit.discover import Found, discover, in_as_of_order
from sbh_data_bank.commit.engine import CommitEngine, CommitOutcome, CommitResult
from sbh_data_bank.commit.memory import MemoryStore
from sbh_data_bank.commit.records import CurrentQuery
from sbh_data_bank.commit.sql import (
    SqlStore,
    curated_columns,
    curated_input_sizes,
    current_rows_sql,
    input_size,
    landed_columns,
    landed_input_sizes,
    supersede_sql,
    typed_columns,
    typed_input_sizes,
)
from sbh_data_bank.curate.delivery import Coverage
from sbh_data_bank.ingest.load import LoadOutcome
from sbh_data_bank.registry.loader import SpecRegistry
from sbh_data_bank.registry.spec import CoverageSource, SourceSpec
from sbh_data_bank.store.schema import curated_statements, landed_statements, typed_statements

CLOCK = datetime(2026, 10, 20, 12, 0, 0)
HEADER = "ID,Account,Event,Amount,Note"


def _spec(**overrides) -> SourceSpec:
    base = {
        "key": "t.vendor.report",
        "name": "n",
        "vendor": "v",
        "natural_key": ["id"],
        "event_date": "event",
        "coverage": "filename",
        "vanish_scope": ["account"],
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
                    {
                        "target": "amount",
                        "sources": ["Amount"],
                        "type": "currency",
                        "control_total": True,
                    },
                    {"target": "note", "sources": ["Note"]},
                ],
            }
        ],
    }
    return SourceSpec.model_validate({**base, **overrides})


@pytest.fixture(scope="module")
def spec() -> SourceSpec:
    return _spec()


def _write(folder: Path, name: str, *rows: str, header: str = HEADER) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def _engine(store: MemoryStore) -> CommitEngine:
    return CommitEngine(
        store, tenant_id="t1", actor="tester", clock=lambda: CLOCK, deterministic_ids=True
    )


def _commit(engine: CommitEngine, spec: SourceSpec, path: Path, root: Path) -> CommitResult:
    (result,) = engine.commit_path(spec, path, run_id="run1", root=root)
    return result


JAN = "t_01012026-01312026.csv"
FEB = "t_01012026-02282026.csv"
A_X = "A,X,2026-01-10,10.00,a"
B_X = "B,X,2026-01-12,20.00,b"
C_Y = "C,Y,2026-01-15,30.00,c"


class TestFirstLoad:
    def test_everything_lands_and_the_batch_accounts_for_every_row(self, tmp_path, spec):
        store = MemoryStore()
        path = _write(tmp_path, JAN, A_X, B_X, C_Y)
        result = _commit(_engine(store), spec, path, tmp_path)

        assert result.committed and result.outcome == CommitOutcome.LOADED
        assert result.outcomes.inserted == 3
        assert len(store.landed[spec.key]) == 3
        assert len(store.typed[spec.key]) == 3
        assert [r["id"] for r in store.live(spec.key)] == ["A", "B", "C"]
        batch = store.batches[result.batch_id]
        assert (batch.rows_read, batch.rows_landed, batch.rows_inserted) == (3, 3, 3)
        assert batch.accounted == batch.rows_read
        assert batch.outcome == "LOADED"
        assert '"amount": "60.00"' in batch.control_totals

    def test_the_file_is_registered_with_its_delivery_facts(self, tmp_path, spec):
        store = MemoryStore()
        path = _write(tmp_path, JAN, A_X)
        result = _commit(_engine(store), spec, path, tmp_path)
        record = store.source_files[result.source_file_id]

        assert record.file_name == JAN
        assert (record.as_of, record.as_of_source) == (date(2026, 1, 31), "filename")
        assert (record.coverage_from, record.coverage_to) == (date(2026, 1, 1), date(2026, 1, 31))
        assert record.delivery_rank == 1
        assert '"target": "id"' in record.header_json

    def test_typed_values_reach_every_layer_typed(self, tmp_path, spec):
        store = MemoryStore()
        _commit(_engine(store), spec, _write(tmp_path, JAN, A_X), tmp_path)
        (landed,), (typed,), (curated,) = (
            store.landed[spec.key], store.typed[spec.key], store.live(spec.key),
        )  # fmt: skip
        assert landed["amount"] == "10.00"  # as delivered: text
        assert typed["amount"] == Decimal("10.00")
        assert curated["amount"] == Decimal("10.00")
        assert curated["event"] == date(2026, 1, 10)
        assert curated["outcome"] == "inserted"
        assert curated["as_of"] == date(2026, 1, 31)
        assert (curated["created_by"], curated["created_at"]) == ("tester", CLOCK)
        assert typed["spec_sha256"] and len(typed["row_hash"]) == 64


class TestOutcomes:
    def test_a_second_delivery_corrects_one_row_and_leaves_the_other(self, tmp_path, spec):
        store = MemoryStore()
        engine = _engine(store)
        first = _commit(engine, spec, _write(tmp_path, JAN, A_X, B_X), tmp_path)
        second = _commit(
            engine, spec, _write(tmp_path, FEB, "A,X,2026-01-10,11.00,a", B_X), tmp_path
        )

        assert (second.outcomes.corrected, second.outcomes.unchanged) == (1, 1)
        live = {r["id"]: r for r in store.live(spec.key)}
        assert live["A"]["amount"] == Decimal("11.00")
        assert live["A"]["batch_id"] == second.batch_id
        assert live["B"]["batch_id"] == first.batch_id  # unchanged: nothing rewritten
        (old,) = [r for r in store.curated[spec.key] if r["superseded_by"] is not None]
        assert (old["id"], old["supersede_reason"], old["superseded_by"]) == (
            "A", "corrected", second.batch_id,
        )  # fmt: skip
        assert (old["updated_by"], old["updated_at"]) == ("tester", CLOCK)
        # Unchanged rows are counted, not written: 2 inserts + 1 correction.
        assert len(store.curated[spec.key]) == 3
        assert store.batches[second.batch_id].rows_superseded == 1

    def test_an_older_delivery_is_history_behind_the_current_row(self, tmp_path, spec):
        store = MemoryStore()
        engine = _engine(store)
        newer = _commit(engine, spec, _write(tmp_path, FEB, "A,X,2026-01-10,11.00,a"), tmp_path)
        older = _commit(engine, spec, _write(tmp_path, JAN, A_X), tmp_path)

        assert older.outcomes.late == 1
        (live,) = store.live(spec.key)
        assert live["amount"] == Decimal("11.00")
        (late,) = [r for r in store.curated[spec.key] if r["outcome"] == "late"]
        assert late["amount"] == Decimal("10.00")
        assert (late["superseded_by"], late["supersede_reason"]) == (newer.batch_id, "late")
        assert store.batches[older.batch_id].rows_late == 1

    def test_a_conflict_holds_the_whole_batch_and_writes_nothing(self, tmp_path, spec):
        store = MemoryStore()
        engine = _engine(store)
        _commit(engine, spec, _write(tmp_path, "a_" + JAN, A_X, B_X), tmp_path)
        before = store.snapshot()
        held = _commit(
            engine, spec, _write(tmp_path, "b_" + JAN, "A,X,2026-01-10,99.00,a", C_Y), tmp_path
        )

        assert held.outcome == CommitOutcome.HELD_CONFLICT
        assert (held.outcomes.conflict, held.outcomes.inserted) == (1, 1)
        after = store.snapshot()
        for table in ("landed", "typed", "curated", "quarantine"):
            assert after[table] == before[table]
        batch = store.batches[held.batch_id]
        assert (batch.rows_conflict, batch.rows_inserted) == (1, 0)
        assert held.source_file_id in store.source_files

    def test_reloading_the_same_file_changes_nothing(self, tmp_path, spec):
        store = MemoryStore()
        engine = _engine(store)
        path = _write(tmp_path, JAN, A_X, B_X)
        first = _commit(engine, spec, path, tmp_path)
        before = store.snapshot()
        again = _commit(engine, spec, path, tmp_path)

        assert again.outcome == CommitOutcome.DUPLICATE_FILE
        assert again.source_file_id == first.source_file_id
        after = store.snapshot()
        for table in ("landed", "typed", "curated", "source_files"):
            assert after[table] == before[table]
        assert len(store.batches) == 2

    def test_a_refused_file_is_not_a_duplicate_when_it_comes_back_renamed(self, tmp_path, spec):
        """Only a LOADED batch makes a hash a duplicate: an operator who
        fixes the name of a refused file must be able to load it."""
        store = MemoryStore()
        engine = _engine(store)
        bad = _commit(engine, spec, _write(tmp_path / "2026-01-31", "t_jan.csv", A_X), tmp_path)
        assert bad.outcome == CommitOutcome.HELD_COVERAGE_UNKNOWN
        good = _commit(engine, spec, _write(tmp_path, JAN, A_X), tmp_path)
        assert good.committed and good.source_file_id == bad.source_file_id

    def test_replaying_the_same_files_gives_the_same_state(self, tmp_path, spec):
        files = [_write(tmp_path, JAN, A_X, B_X), _write(tmp_path, FEB, A_X, C_Y)]
        states = []
        for _ in range(2):
            store = MemoryStore()
            engine = _engine(store)
            for path in files:
                _commit(engine, spec, path, tmp_path)
            states.append(store.snapshot())
        assert states[0] == states[1]


class TestVanish:
    def test_a_covered_absent_row_of_a_carried_account_vanishes(self, tmp_path, spec):
        store = MemoryStore()
        engine = _engine(store)
        _commit(engine, spec, _write(tmp_path, JAN, A_X, B_X, C_Y), tmp_path)
        feb = _commit(engine, spec, _write(tmp_path, FEB, A_X), tmp_path)

        assert feb.outcomes.vanished == 1
        assert sorted(r["id"] for r in store.live(spec.key)) == ["A", "C"]
        (gone,) = [r for r in store.curated[spec.key] if r["supersede_reason"] == "vanished"]
        assert (gone["id"], gone["superseded_by"]) == ("B", feb.batch_id)
        assert store.batches[feb.batch_id].rows_vanished == 1

    def test_a_row_outside_the_window_survives(self, tmp_path, spec):
        store = MemoryStore()
        engine = _engine(store)
        _commit(engine, spec, _write(tmp_path, JAN, A_X, B_X), tmp_path)
        march = _commit(
            engine,
            spec,
            _write(tmp_path, "t_02012026-03312026.csv", "D,X,2026-03-01,5,d"),
            tmp_path,
        )
        assert march.outcomes.vanished == 0
        assert sorted(r["id"] for r in store.live(spec.key)) == ["A", "B", "D"]


class TestRefusals:
    def test_a_row_dated_after_the_as_of_holds_the_batch(self, tmp_path, spec):
        store = MemoryStore()
        result = _commit(
            _engine(store), spec, _write(tmp_path, JAN, "A,X,2026-02-15,1,a"), tmp_path
        )
        assert result.outcome == CommitOutcome.HELD_AS_OF_FLOOR
        assert store.live(spec.key) == [] and store.landed[spec.key] == []
        assert store.batches[result.batch_id].outcome == CommitOutcome.HELD_AS_OF_FLOOR

    def test_a_name_claiming_past_the_as_of_holds_the_batch(self, tmp_path, spec):
        """D12: the folder says January 31, the name says through March."""
        store = MemoryStore()
        path = _write(tmp_path / "drop 2026-01-31", "t_01012026-03312026.csv", A_X)
        result = _commit(_engine(store), spec, path, tmp_path)
        assert result.outcome == CommitOutcome.HELD_FILENAME_WINDOW
        assert "claims coverage to 2026-03-31" in result.detail

    def test_a_row_before_the_claimed_start_holds_the_batch(self, tmp_path, spec):
        store = MemoryStore()
        path = _write(tmp_path, "t_01152026-01312026.csv", "A,X,2026-01-05,1,a")
        result = _commit(_engine(store), spec, path, tmp_path)
        assert result.outcome == CommitOutcome.HELD_FILENAME_WINDOW
        assert "1 rows have event before the claimed start 2026-01-15" in result.detail

    def test_a_window_much_wider_than_the_data_is_a_warning(self, tmp_path, spec):
        store = MemoryStore()
        path = _write(tmp_path, "t_01012025-01312026.csv", A_X)
        result = _commit(_engine(store), spec, path, tmp_path)
        assert result.committed
        assert any("starts 2025-01-01" in w for w in result.warnings)
        assert "starts 2025-01-01" in store.batches[result.batch_id].control_totals

    def test_no_as_of_is_refused_not_guessed(self, tmp_path, spec):
        store = MemoryStore()
        result = _commit(_engine(store), spec, _write(tmp_path, "t_jan.csv", A_X), tmp_path)
        assert result.outcome == CommitOutcome.AS_OF_UNAVAILABLE
        assert "no as-of date" in result.detail
        assert store.source_files[result.source_file_id].as_of is None

    def test_a_load_that_is_not_ok_is_recorded_and_nothing_lands(self, tmp_path, spec):
        store = MemoryStore()
        path = _write(tmp_path, JAN, "x,y", header="Foo,Bar")
        result = _commit(_engine(store), spec, path, tmp_path)
        assert result.outcome == LoadOutcome.UNRECOGNISED
        assert store.landed[spec.key] == []
        assert result.source_file_id in store.source_files


class TestZipAndDiscovery:
    """Monthly Payments ships forty months as CSV members of one zip; a
    backfill must find them, order them and commit each as its own file."""

    def _zip(self, folder: Path, name: str, members: dict[str, str]) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        with zipfile.ZipFile(path, "w") as archive:
            for member, body in members.items():
                archive.writestr(member, body)
        return path

    def test_a_zip_member_commits_like_a_file(self, tmp_path, spec):
        store = MemoryStore()
        engine = _engine(store)
        path = self._zip(tmp_path / "drop", "t.zip", {JAN: "\n".join([HEADER, A_X, B_X]) + "\n"})
        (result,) = engine.commit_path(spec, path, run_id="run1", root=tmp_path, member=JAN)
        assert result.outcome == CommitOutcome.LOADED
        assert result.outcomes.inserted == 2
        found = store.source_files[result.source_file_id]
        assert found.file_name == JAN
        assert found.delivery == "drop/t.zip"
        assert found.as_of == date(2026, 1, 31)
        assert found.file_bytes == len(HEADER) + len(A_X) + len(B_X) + 3

    def test_two_members_with_the_same_bytes_are_one_file(self, tmp_path, spec):
        engine = _engine(MemoryStore())
        body = "\n".join([HEADER, A_X]) + "\n"
        path = self._zip(tmp_path, "t.zip", {JAN: body, "copy/" + JAN: body})
        (first,) = engine.commit_path(spec, path, run_id="run1", root=tmp_path, member=JAN)
        (again,) = engine.commit_path(
            spec, path, run_id="run1", root=tmp_path, member="copy/" + JAN
        )
        assert (first.outcome, again.outcome) == (
            CommitOutcome.LOADED,
            CommitOutcome.DUPLICATE_FILE,
        )

    def test_discover_finds_files_and_zip_members_and_skips_test_folders(self, tmp_path, spec):
        _write(tmp_path / "2026-01-31", JAN, A_X)
        _write(tmp_path / "Sagebrush ETL Test Files", JAN, A_X)
        _write(tmp_path, "other.csv", A_X)
        self._zip(tmp_path, "t.zip", {FEB: "", "readme.txt": ""})
        self._zip(tmp_path, "unrelated.zip", {JAN: ""})
        found = discover(_spec(filename_patterns=["^t"]), tmp_path)
        assert [f.display for f in found] == [JAN, f"t.zip::{FEB}"]
        assert found[1].filename == FEB

    def test_in_as_of_order_puts_the_oldest_first_and_the_undated_last(self, tmp_path, spec):
        late = _write(tmp_path, FEB, A_X)
        early = _write(tmp_path, JAN, A_X)
        undated = _write(tmp_path, "t_nodate.csv", A_X)
        ordered = in_as_of_order(spec, [Found(undated), Found(late), Found(early)], tmp_path)
        assert [f.path.name for _, f in ordered] == [JAN, FEB, "t_nodate.csv"]
        assert [a.value if a else None for a, _ in ordered] == [
            date(2026, 1, 31),
            date(2026, 2, 28),
            None,
        ]


class TestQuarantineAndFindings:
    def test_a_bad_row_is_quarantined_and_still_lands(self, tmp_path, spec):
        store = MemoryStore()
        path = _write(tmp_path, JAN, A_X, "B,X,not-a-date,20.00,b")
        result = _commit(_engine(store), spec, path, tmp_path)

        assert result.committed
        (q,) = store.quarantine
        assert (q["line_number"], q["reasons"]) == (3, "BAD_DATE")
        assert '"raw": "not-a-date"' in q["errors"]
        assert len(store.landed[spec.key]) == 2  # the bad line is in landed too
        assert len(store.typed[spec.key]) == 1
        batch = store.batches[result.batch_id]
        assert (batch.rows_read, batch.rows_quarantined, batch.rows_inserted) == (2, 1, 1)
        assert batch.accounted == batch.rows_read

    def test_lines_sharing_an_identity_are_occurrences_and_a_finding(self, tmp_path, spec):
        store = MemoryStore()
        path = _write(tmp_path, JAN, A_X, "A,X,2026-01-10,12.00,second")
        result = _commit(_engine(store), spec, path, tmp_path)

        assert result.outcomes.inserted == 2
        assert sorted(r["occurrence"] for r in store.live(spec.key)) == [1, 2]
        (finding,) = store.findings
        assert (finding["line_numbers"], finding["identical"]) == ("2,3", False)
        assert '"id": "A"' in finding["key_values"]


class TestTransaction:
    def test_a_store_failure_leaves_nothing_behind(self, tmp_path, spec):
        class Failing(MemoryStore):
            def write(self, *args, **kwargs):
                super().write(*args, **kwargs)
                raise RuntimeError("disk on fire")

        store = Failing()
        before = store.snapshot()
        with pytest.raises(RuntimeError, match="disk on fire"):
            _commit(_engine(store), spec, _write(tmp_path, JAN, A_X), tmp_path)
        assert store.snapshot() == before


# ---------------------------------------------------------------- SQL store


def _ddl_columns(statement: str) -> list[str]:
    """Column names of a generated CREATE TABLE, in order."""
    names = []
    for line in statement.splitlines():
        if not line.startswith("    ") or line.strip().startswith("CONSTRAINT"):
            continue
        names.append(re.match(r"\s*\[?(\w+)\]?", line).group(1))
    return names


class TestSqlStore:
    def test_insert_column_lists_match_the_ddl(self, spec):
        assert landed_columns(spec) == _ddl_columns(landed_statements(spec)[0])
        assert typed_columns(spec) == _ddl_columns(typed_statements(spec)[0])
        assert curated_columns(spec) == _ddl_columns(curated_statements(spec)[0])

    def test_supersede_only_touches_a_live_row(self, spec):
        sql = supersede_sql(spec)
        assert sql.startswith("UPDATE curated.t_vendor_report SET superseded_by = ?")
        assert sql.endswith("AND superseded_by IS NULL")

    def test_current_rows_query_joins_identities_and_the_window(self, spec):
        query = CurrentQuery(
            keys=frozenset({("h", 1)}),
            coverage=Coverage(date(2026, 1, 1), date(2026, 1, 31), CoverageSource.FILENAME),
            coverage_column="event",
            event_column="event",
            scope_columns=("account",),
            control_columns=("amount",),
        )
        sql, params = current_rows_sql(spec, query)
        assert "FROM curated.t_vendor_report AS c" in sql
        assert "EXISTS (SELECT 1 FROM #incoming" in sql
        assert "c.[event] >= ? AND c.[event] <= ?" in sql
        assert params == [date(2026, 1, 1), date(2026, 1, 31)]
        assert sql.split(" FROM ")[0].endswith("c.as_of, c.[event], c.[account], c.[amount]")

    def test_one_commit_runs_the_expected_statements(self, tmp_path, spec):
        cursor = _FakeCursor()
        engine = CommitEngine(
            SqlStore(_FakeDatabase(cursor)),
            tenant_id="t1",
            actor="tester",
            clock=lambda: CLOCK,
            deterministic_ids=True,
        )
        path = _write(tmp_path, JAN, A_X, "A,X,2026-01-10,12.00,dup", "B,X,bad,1,b")
        (result,) = engine.commit_path(spec, path, run_id="run1", root=tmp_path)

        assert result.committed
        heads = [sql.split(" (")[0].split(" AS ")[0][:36] for sql, _ in cursor.calls]
        assert heads == [
            "SELECT sf.source_file_id,",
            "SELECT COUNT(*) + 1 FROM meta.source",
            "INSERT INTO meta.source_file",
            "INSERT INTO meta.batch",
            "CREATE TABLE #incoming",
            "INSERT INTO #incoming",
            "SELECT c.batch_id, c.source_file_id,",
            "DROP TABLE #incoming",
            "INSERT INTO landed.t_vendor_report",
            "INSERT INTO typed.t_vendor_report",
            "INSERT INTO curated.t_vendor_report",
            "INSERT INTO stg.quarantine",
            "INSERT INTO stg.duplicate_finding",
            "UPDATE meta.batch SET source_key = ?",
        ]
        select = next(sql for sql, _ in cursor.calls if sql.startswith("SELECT c.batch_id"))
        assert "c.as_of, c.[event], c.[account], c.[amount] FROM curated.t_vendor_report" in select
        many = {sql.split(" (")[0]: rows for sql, rows in cursor.many}
        assert len(many["INSERT INTO landed.t_vendor_report"]) == 3
        assert len(many["INSERT INTO typed.t_vendor_report"]) == 2
        assert len(many["INSERT INTO curated.t_vendor_report"]) == 2
        assert len(many["INSERT INTO landed.t_vendor_report"][0]) == len(landed_columns(spec))
        assert len(many["INSERT INTO curated.t_vendor_report"][0]) == len(curated_columns(spec))
        assert cursor.commits == 1

    def test_a_supersession_that_hits_no_live_row_raises(self, spec):
        cursor = _FakeCursor(rowcount=0)
        store = SqlStore(_FakeDatabase(cursor))
        from sbh_data_bank.commit.records import Supersession, WriteBatch, WriteContext

        ctx = WriteContext("t1", "b2", 1, "tester", CLOCK)
        batch = WriteBatch(supersessions=[Supersession(("b1", 1, 2), "corrected")])
        with pytest.raises(RuntimeError, match="not live"), store.transaction():
            store.write(spec, ctx, batch)
        assert cursor.commits == 0


class _FakeCursor:
    """Records every statement; answers the reads the engine makes."""

    def __init__(self, rowcount: int = 1) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.many: list[tuple[str, list]] = []
        self.rowcount = rowcount
        self.fast_executemany = False
        self._next: list[tuple] = []

    def execute(self, sql: str, *params):
        self.calls.append((sql, params))
        # A new file: not found, inserted as id 1, rank 1, no live rows.
        answers = (("INSERT INTO meta.source_file", [(1,)]), ("SELECT COUNT(*) + 1", [(1,)]))
        self._next = next((rows for head, rows in answers if sql.startswith(head)), [])

    def executemany(self, sql: str, rows):
        self.calls.append((sql, ("<many>",)))
        self.many.append((sql, list(rows)))

    def setinputsizes(self, sizes):
        self.calls.append(("<setinputsizes>", (list(sizes),)))

    def fetchone(self):
        return self._next[0] if self._next else None

    def fetchall(self):
        return list(self._next)


class _FakeDatabase:
    def __init__(self, cursor: _FakeCursor) -> None:
        self.cursor = cursor
        self.cursor.commits = 0

    @contextmanager
    def transaction(self):
        yield self.cursor
        self.cursor.commits += 1


class TestFastPathDeclaresEveryParameter:
    """With fast_executemany on, pyodbc guesses parameter types from the data
    and has rounded DECIMAL scale and mis-sized NVARCHAR(MAX) before. The
    store therefore declares every parameter from the DDL, and this is the
    check that the declaration and the INSERT column list agree, column for
    column, for every shipped spec."""

    @pytest.fixture
    def registry(self):
        return SpecRegistry.from_dir(Path(__file__).resolve().parents[2] / "specs")

    def test_one_declared_type_per_insert_column_for_every_spec(self, registry):
        for spec in registry:
            assert len(landed_input_sizes(spec)) == len(landed_columns(spec)), spec.key
            assert len(typed_input_sizes(spec)) == len(typed_columns(spec)), spec.key
            assert len(curated_input_sizes(spec)) == len(curated_columns(spec)), spec.key

    def test_the_ddl_tokens_map_to_odbc_declarations(self):
        assert input_size("NVARCHAR(MAX)") == (-9, 0, 0)
        assert input_size("NVARCHAR(64)") == (-9, 64, 0)
        assert input_size("CHAR(64)") == (1, 64, 0)
        assert input_size("DECIMAL(38,4)") == (3, 38, 4)
        for inferred in ("BIGINT", "INT", "DATE", "BIT", "DATETIME2(3)"):
            assert input_size(inferred) is None

    def test_money_columns_declare_their_scale(self, registry):
        spec = registry.get("wholesaler.mckesson.invoice")
        sizes = dict(zip(typed_columns(spec), typed_input_sizes(spec), strict=True))
        assert sizes["extended_price"] == (3, 38, 4)
        assert sizes["row_hash"] == (1, 64, 0)

    def _run(self, tmp_path, fast: bool) -> tuple[_FakeCursor, CommitResult]:
        spec = _spec()
        cursor = _FakeCursor()
        engine = CommitEngine(
            SqlStore(_FakeDatabase(cursor), fast_executemany=fast),
            tenant_id="t1",
            actor="tester",
            clock=lambda: CLOCK,
            deterministic_ids=True,
        )
        path = _write(tmp_path, JAN, A_X, B_X)
        (result,) = engine.commit_path(spec, path, run_id="run1", root=tmp_path)
        return cursor, result

    def test_a_fast_store_declares_before_each_batch_and_clears_after(self, tmp_path):
        cursor, result = self._run(tmp_path, fast=True)
        assert result.committed
        assert cursor.fast_executemany is True
        shapes = [params[0] for sql, params in cursor.calls if sql == "<setinputsizes>"]
        inserts = [sql for sql, params in cursor.calls if params == ("<many>",)]
        assert inserts
        # Every batch insert is preceded by a declaration and followed by a reset.
        assert len([s for s in shapes if s]) == len(inserts)
        assert len([s for s in shapes if not s]) == len(inserts)
        spec = _spec()
        landed = next(s for s in shapes if s and len(s) == len(landed_columns(spec)))
        # Every landed cell, plus extra_cells, is declared NVARCHAR(MAX).
        assert landed.count((-9, 0, 0)) == len(spec.landed_targets) + 1

    def test_a_slow_store_never_declares(self, tmp_path):
        cursor, result = self._run(tmp_path, fast=False)
        assert result.committed
        assert not [1 for sql, _ in cursor.calls if sql == "<setinputsizes>"]
