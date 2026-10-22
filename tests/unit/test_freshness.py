"""The freshness check (``observe.freshness``, ``observe.sql``).

``expected_cadence_days`` is a staleness threshold, not a schedule, and
the grace period is what keeps a Sunday-morning check from firing before
the upload. These tests pin ``today`` and hold the rules: when a weekly
source is stale, which sources are never alerted on, what counts as an
open refusal, and which findings are alerts. The SQL side is run through
a fake database that returns canned rows, so the shape of every query's
result is tested without a server.
"""

from __future__ import annotations

from datetime import date, datetime

from sbh_data_bank.observe.freshness import (
    BENIGN_OUTCOMES,
    Finding,
    RunStatus,
    SourceStatus,
    assess,
    stale_after,
)
from sbh_data_bank.observe.sql import (
    LOADED_BY_SOURCE,
    OPEN_QUARANTINE,
    OPEN_REFUSALS,
    RUN_SOURCES,
    RUNS,
    SEEN_BY_SOURCE,
    collect,
)
from sbh_data_bank.registry.spec import SourceSpec

TODAY = date(2026, 11, 3)  # a Tuesday


def _spec(key="t.weekly", **overrides) -> SourceSpec:
    base = {
        "key": key,
        "name": "n",
        "vendor": "v",
        "natural_key": ["id"],
        "expected_cadence_days": 7,
        "variants": [
            {
                "name": "only",
                "detect": {"require_all": ["ID"]},
                "columns": [{"target": "id", "sources": ["ID"], "required": True}],
            }
        ],
    }
    return SourceSpec.model_validate({**base, **overrides})


def _kinds(findings: list[Finding]) -> list[tuple[str, str | None]]:
    return [(f.kind, f.source_key) for f in findings]


class TestStaleAfter:
    def test_cadence_plus_grace(self):
        assert stale_after(_spec()) == 9
        assert stale_after(_spec(), grace_days=0) == 7

    def test_no_cadence_or_not_active_is_never_stale(self):
        assert stale_after(_spec(expected_cadence_days=None)) is None
        assert stale_after(_spec(lifecycle="discontinued")) is None
        assert stale_after(_spec(lifecycle="no_access")) is None


class TestStaleness:
    def test_a_weekly_source_is_stale_on_the_tuesday_after_a_missed_sunday(self):
        spec = _spec()
        sunday = date(2026, 10, 25)
        loaded = {spec.key: SourceStatus(spec.key, latest_loaded_as_of=sunday, loaded_files=1)}
        # The following Sunday (7) and Monday (8) are inside the grace.
        for today in (date(2026, 11, 1), date(2026, 11, 2)):
            assert assess([spec], loaded, [], today=today) == []
        (finding,) = assess([spec], loaded, [], today=date(2026, 11, 3))
        assert (finding.kind, finding.source_key, finding.is_alert) == ("STALE", spec.key, True)
        assert "as of 2026-10-25 (9 days)" in finding.message
        assert "every 7 days plus 2 grace" in finding.message

    def test_the_grace_is_a_parameter(self):
        spec = _spec()
        loaded = {spec.key: SourceStatus(spec.key, latest_loaded_as_of=date(2026, 10, 25))}
        assert _kinds(assess([spec], loaded, [], today=TODAY, grace_days=5)) == []
        assert _kinds(assess([spec], loaded, [], today=TODAY, grace_days=1)) == [
            ("STALE", spec.key)
        ]

    def test_a_source_that_never_loaded_is_its_own_finding(self):
        spec = _spec()
        (finding,) = assess([spec], {}, [], today=TODAY)
        assert finding.kind == "NEVER_LOADED" and finding.is_alert

    def test_sources_without_an_expectation_are_silent(self):
        specs = [
            _spec("t.reference", expected_cadence_days=None),
            _spec("t.gone", lifecycle="discontinued"),
        ]
        assert assess(specs, {}, [], today=TODAY) == []

    def test_a_held_file_is_not_a_delivery(self):
        """Only a LOADED delivery's as-of counts; the held one is listed as
        an open refusal instead, which is the alert that says why."""
        spec = _spec()
        status = SourceStatus(
            spec.key,
            latest_loaded_as_of=date(2026, 10, 18),
            open_refusals=(("t_oct.csv", "HELD_QUARANTINE_RATE", datetime(2026, 11, 1, 9)),),
        )
        findings = assess([spec], {spec.key: status}, [], today=TODAY)
        assert _kinds(findings) == [("STALE", spec.key), ("OPEN_REFUSAL", spec.key)]
        assert "t_oct.csv: HELD_QUARANTINE_RATE on 2026-11-01 09:00" in findings[1].message


class TestOtherFindings:
    def test_open_quarantine_is_a_note_not_an_alert(self):
        spec = _spec()
        status = SourceStatus(spec.key, latest_loaded_as_of=TODAY, open_quarantine=4)
        (finding,) = assess([spec], {spec.key: status}, [], today=TODAY)
        assert finding.kind == "OPEN_QUARANTINE" and not finding.is_alert
        assert "4 quarantined rows" in finding.message

    def test_a_failed_run_inside_the_window_is_an_alert(self):
        now = datetime(2026, 11, 3, 8)
        old, new, ok = datetime(2026, 10, 20), datetime(2026, 11, 2), datetime(2026, 11, 1)
        runs = [
            RunStatus("run_old", "backfill", old, old, "FAILED"),
            RunStatus("run_new", "backfill", new, new, "FAILED", "OSError: boom"),
            RunStatus("run_ok", "manual", ok, ok, "LOADED"),
        ]
        (finding,) = assess([], {}, runs, today=TODAY, now=now)
        assert finding.kind == "RUN_FAILED" and finding.source_key is None
        assert finding.message == "run_new (backfill) started 2026-11-02 00:00: OSError: boom"

    def test_a_later_successful_run_over_the_same_sources_closes_a_failure(self):
        """The backfill's patients run failed on its sixth file and was
        re-run once the column was widened; the re-run closes the alert.
        A failure that reached no source is closed by any later load."""
        now = datetime(2026, 11, 3, 8)
        failed = RunStatus(
            "run_f",
            "backfill",
            datetime(2026, 11, 1, 1),
            datetime(2026, 11, 1, 1),
            "FAILED",
            "truncation",
            frozenset({"t.patients"}),
        )
        other = RunStatus(
            "run_o",
            "backfill",
            datetime(2026, 11, 1, 2),
            datetime(2026, 11, 1, 2),
            "LOADED",
            None,
            frozenset({"t.claims"}),
        )
        rerun = RunStatus(
            "run_r",
            "backfill",
            datetime(2026, 11, 1, 3),
            datetime(2026, 11, 1, 3),
            "LOADED",
            None,
            frozenset({"t.patients", "t.claims"}),
        )
        blank = RunStatus("run_b", "manual", datetime(2026, 11, 2), datetime(2026, 11, 2), "FAILED")
        open_failure = [("RUN_FAILED", None)]
        assert _kinds(assess([], {}, [failed, other], today=TODAY, now=now)) == open_failure
        assert assess([], {}, [failed, other, rerun], today=TODAY, now=now) == []
        assert _kinds(assess([], {}, [blank], today=TODAY, now=now)) == [("RUN_FAILED", None)]
        assert assess([], {}, [blank, RunStatus("run_l", "manual", datetime(2026, 11, 2, 1),
                                                datetime(2026, 11, 2, 1), "LOADED")],
                      today=TODAY, now=now) == []  # fmt: skip

    def test_an_open_run_is_unfinished_only_after_the_hours(self):
        now = datetime(2026, 11, 3, 8)
        recent = RunStatus("run_live", "backfill", datetime(2026, 11, 3, 7), None, None)
        stale = RunStatus("run_dead", "backfill", datetime(2026, 11, 2, 7), None, None)
        findings = assess([], {}, [recent, stale], today=TODAY, now=now)
        assert _kinds(findings) == [("RUN_UNFINISHED", None)]
        assert "run_dead" in findings[0].message and "close_runs.py" in findings[0].message

    def test_findings_print_in_three_columns(self):
        line = str(Finding("STALE", "t.weekly", "late"))
        assert line.startswith("STALE") and "t.weekly" in line and line.endswith("late")


class _FakeDb:
    """Returns canned rows per query and records the parameters it saw."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, *params):
        self.calls.append((sql, params))
        return self.rows.get(sql, [])


class TestCollect:
    def test_statuses_are_merged_from_the_four_queries(self):
        seen = datetime(2026, 10, 22, 18, 0)
        db = _FakeDb(
            {
                LOADED_BY_SOURCE: [("t.a", date(2026, 10, 22), 6), ("t.b", date(2026, 7, 21), 40)],
                SEEN_BY_SOURCE: [("t.a", seen), ("t.b", seen), ("t.c", seen)],
                OPEN_REFUSALS: [("t.c", "c.csv", None, "UNRECOGNISED", seen),
                                ("t.c", "c.xlsx", "Sheet1", "HELD_AS_OF_FLOOR", seen)],
                OPEN_QUARANTINE: [("t.b", 3)],
                RUNS: [("run1", "backfill", seen, None, None, None)],
                RUN_SOURCES: [("run1", "t.a"), ("run1", "t.b")],
            }
        )  # fmt: skip
        statuses, runs = collect(db, "sagebrush", since=datetime(2026, 10, 15))

        assert statuses["t.a"] == SourceStatus("t.a", date(2026, 10, 22), seen, 6, (), 0)
        assert statuses["t.b"].open_quarantine == 3 and statuses["t.b"].loaded_files == 40
        c = statuses["t.c"]
        assert c.latest_loaded_as_of is None and c.loaded_files == 0
        assert c.open_refusals == (
            ("c.csv", "UNRECOGNISED", seen),
            ("c.xlsx [Sheet1]", "HELD_AS_OF_FLOOR", seen),
        )
        assert runs == [
            RunStatus("run1", "backfill", seen, None, None, None, frozenset({"t.a", "t.b"}))
        ]
        # Every query is tenant-scoped; the run query also takes the window.
        assert all(params[0] == "sagebrush" for _, params in db.calls)
        assert [p for sql, p in db.calls if sql == RUNS] == [("sagebrush", datetime(2026, 10, 15))]

    def test_the_refusal_query_excludes_every_benign_outcome(self):
        for outcome in BENIGN_OUTCOMES:
            assert f"'{outcome}'" in OPEN_REFUSALS
        assert "NOT IN" in OPEN_REFUSALS and "MAX(b2.started_at)" in OPEN_REFUSALS
