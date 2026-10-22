"""Typing a loaded file again from its landed rows (``ingest.replay``,
``commit.retype``).

The claim under test is the one the layered design rests on: the landed
layer plus the recorded header is enough to reproduce the typed layer
without the file. So each test commits real files through the engine into
the in-memory store, reads the landed rows back and types them again, and
holds the result against what the first load wrote: the same row hashes,
the same quarantine, the same control totals. The workbook case matters
most, because a date cell lands as ``str(datetime)`` and only the replay's
one reversal makes it parse again. A spec edit then shows up as the
difference it is.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from sbh_data_bank.commit.engine import CommitEngine
from sbh_data_bank.commit.memory import MemoryStore
from sbh_data_bank.commit.retype import replay_source
from sbh_data_bank.commit.sql import landed_select_sql
from sbh_data_bank.ingest.replay import LandedLine, header_from_json, rebuild_rows
from sbh_data_bank.registry.loader import SpecRegistry
from sbh_data_bank.registry.spec import SourceSpec
from sbh_data_bank.store.schema import landed_statements

CLOCK = datetime(2026, 10, 22, 12, 0, 0)
HEADER = "ID,Account,Event,Amount,SSN,Note,Memo"
SPEC_DIR = Path(__file__).resolve().parents[2] / "specs"


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
                    {
                        "target": "amount",
                        "sources": ["Amount"],
                        "type": "currency",
                        "control_total": True,
                    },
                    {"target": "ssn", "sources": ["SSN"], "pii": "prohibited"},
                    {"target": "note", "sources": ["Note"]},
                ],
            }
        ],
    }
    return SourceSpec.model_validate({**base, **overrides})


def _write(folder: Path, name: str, *rows: str) -> Path:
    path = folder / name
    path.write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")
    return path


def _engine(store: MemoryStore) -> CommitEngine:
    return CommitEngine(
        store, tenant_id="t1", actor="tester", clock=lambda: CLOCK, deterministic_ids=True
    )


JAN = "t_01012026-01312026.csv"
FEB = "t_01012026-02282026.csv"
A = "A,X,2026-01-10,10.00,123-45-6789,a,m1"
B = "B,X,2026-01-12,20.00,987-65-4321,b,"
BAD = "C,Y,not a date,30.00,000-00-0000,c,m3"


def _loaded(tmp_path: Path, spec: SourceSpec) -> MemoryStore:
    store = MemoryStore()
    engine = _engine(store)
    engine.commit_path(spec, _write(tmp_path, JAN, A, B, BAD), run_id="r", root=tmp_path)
    engine.commit_path(
        spec, _write(tmp_path, FEB, "A,X,2026-01-10,11.00,123-45-6789,a,m1", B), run_id="r",
        root=tmp_path,
    )  # fmt: skip
    return store


class TestRoundTrip:
    def test_a_csv_load_is_reproduced_line_for_line(self, tmp_path):
        spec = _spec()
        store = _loaded(tmp_path, spec)
        replays = list(replay_source(store, spec, "t1"))

        assert [r.file.file_name for r in replays] == [JAN, FEB]
        assert all(r.identical for r in replays), [r.summary() for r in replays]
        first, second = replays
        assert (first.lines, first.unchanged, first.still_quarantined) == (3, 2, 1)
        assert (second.lines, second.unchanged, second.still_quarantined) == (2, 2, 0)
        assert first.totals_agree and second.totals_agree

    def test_the_prohibited_cell_is_gone_and_the_unclaimed_cell_is_back(self, tmp_path):
        """The SSN position reads ``None``; the Memo column, which no spec
        column claimed, comes back from ``extra_cells`` under its header."""
        spec = _spec()
        store = _loaded(tmp_path, spec)
        file = store.loaded_files("t1", spec.key)[0]
        header, targets = header_from_json(file.header_json)
        assert header == HEADER.split(",")
        assert targets == {0: "id", 1: "account", 2: "event", 3: "amount", 4: "ssn", 5: "note"}
        rows = list(rebuild_rows(header, targets, store.landed_lines(spec, 1), sheet=None))
        assert rows[0].values == ("A", "X", "2026-01-10", "10.00", None, "a", "m1")
        assert rows[1].values == ("B", "X", "2026-01-12", "20.00", None, "b", None)
        assert [r.line_number for r in rows] == [2, 3, 4]

    def test_a_workbook_load_is_reproduced_including_its_date_cells(self, tmp_path):
        """openpyxl hands the reader datetimes, which land as
        ``str(datetime)``; the replay turns that one rendering back so the
        declared formats are not asked to parse it."""
        openpyxl = pytest.importorskip("openpyxl")
        spec = _spec(
            filename_patterns=["book"],
            variants=[
                {
                    "name": "only",
                    "detect": {"require_all": ["ID", "Event"]},
                    "read": {"sheet": "Data"},
                    "columns": [
                        {"target": "id", "sources": ["ID"], "required": True},
                        {
                            "target": "event",
                            "sources": ["Event"],
                            "type": "date",
                            "date_formats": ["%m/%d/%Y"],
                            "required": True,
                        },
                        {"target": "amount", "sources": ["Amount"], "type": "currency"},
                        {"target": "units", "sources": ["Units"], "type": "integer"},
                        {"target": "flag", "sources": ["Flag"], "type": "boolean"},
                    ],
                }
            ],
        )
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Data"
        sheet.append(["ID", "Event", "Amount", "Units", "Flag"])
        sheet.append(["A", datetime(2026, 1, 10), 10.5, 3, True])
        sheet.append(["B", datetime(2026, 1, 12, 0, 0, 0, 500000), 20, 4.0, False])
        sheet.append(["C", "01/15/2026", 30, 5, "Y"])
        path = tmp_path / "book_01012026-01312026.xlsx"
        workbook.save(path)

        store = MemoryStore()
        _engine(store).commit_path(spec, path, run_id="r", root=tmp_path)
        (landed_a, landed_b, _) = store.landed[spec.key]
        assert landed_a["event"] == "2026-01-10 00:00:00"
        assert landed_b["event"] == "2026-01-12 00:00:00.500000"

        (replay,) = replay_source(store, spec, "t1")
        assert replay.identical, replay.summary()
        assert (replay.lines, replay.unchanged) == (3, 3)
        file = store.loaded_files("t1", spec.key)[0]
        header, targets = header_from_json(file.header_json)
        rows = list(rebuild_rows(header, targets, store.landed_lines(spec, 1), sheet="Data"))
        assert rows[0].values[1] == datetime(2026, 1, 10)
        assert rows[2].values[1] == "01/15/2026"  # text in the workbook stays text
        assert rows[0].sheet == "Data"

    def test_a_csv_cell_that_looks_like_a_timestamp_stays_text(self):
        rows = list(
            rebuild_rows(
                ["When"],
                {0: "when"},
                [LandedLine(2, {"when": "2026-01-10 00:00:00"}, {})],
                sheet=None,
            )
        )
        assert rows[0].values == ("2026-01-10 00:00:00",)


class TestASpecEditShowsUp:
    def test_excluding_a_column_from_the_hash_rehashes_every_row(self, tmp_path):
        store = _loaded(tmp_path, _spec())
        edited = _spec(row_hash_exclude=["note"])
        first, second = replay_source(store, edited, "t1")

        assert not first.identical and not second.identical
        assert (first.rehashed, first.unchanged, first.still_quarantined) == (2, 0, 1)
        assert first.samples == {"rehashed": [2, 3]}
        assert first.totals_agree

    def test_a_changed_date_format_quarantines_what_typed_before(self, tmp_path):
        store = _loaded(tmp_path, _spec())
        variants = _spec().model_dump()["variants"]
        variants[0]["columns"][2]["date_formats"] = ["%d/%m/%Y"]
        edited = _spec(variants=variants)
        (first, _) = replay_source(store, edited, "t1")

        assert (first.now_quarantined, first.still_quarantined, first.unchanged) == (2, 1, 0)
        assert first.samples == {"now_quarantined": [2, 3]}
        assert not first.totals_agree  # nothing typed, so the sums are zero
        assert "now_quarantined at lines [2, 3]" in first.summary()

    def test_a_header_the_current_spec_cannot_bind_is_unrecognised(self, tmp_path):
        store = _loaded(tmp_path, _spec())
        variants = _spec().model_dump()["variants"]
        variants[0]["detect"]["require_all"] = ["ID", "Invoice"]
        edited = _spec(variants=variants)
        (first, _) = replay_source(store, edited, "t1")

        assert first.outcome == "UNRECOGNISED" and not first.identical
        assert first.lines == 0


class TestSql:
    def test_the_landed_select_names_only_columns_the_table_has(self):
        """The replay's SELECT reads the landed table's business columns in
        ``landed_targets`` order, plus ``extra_cells``, for every spec."""
        for spec in SpecRegistry.from_dir(SPEC_DIR):
            (create,) = landed_statements(spec)
            select = landed_select_sql(spec)
            listed = select.split("SELECT ", 1)[1].split(" FROM")[0]
            columns = [c.strip("[]") for c in listed.split(", ")]
            assert columns[0] == "line_number" and columns[-1] == "extra_cells"
            for name in columns:
                assert f"[{name}]" in create or f"    {name} " in create, (spec.key, name)
            assert "ORDER BY line_number" in select


def test_the_memory_store_lists_only_loaded_files(tmp_path):
    spec = _spec()
    store = _loaded(tmp_path, spec)
    engine = _engine(store)
    # The same bytes again: a DUPLICATE_FILE batch on the same file.
    engine.commit_path(spec, _write(tmp_path, FEB, "A,X,2026-01-10,11.00,123-45-6789,a,m1", B),
                       run_id="r", root=tmp_path)  # fmt: skip
    files = store.loaded_files("t1", spec.key)
    assert [f.file_name for f in files] == [JAN, FEB]
    assert all(f.batch_id.startswith("bat_replay_") for f in files)
    assert files[0].as_of == date(2026, 1, 31)
