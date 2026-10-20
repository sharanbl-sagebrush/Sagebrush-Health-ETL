"""``curate.delivery``: as-of, coverage and the as-of floor.

Decision D5 is the as-of chain - dated folder, spec override, xlsx stamp,
filename, operator - and these tests pin its order, because the order is
what decides which of two overlapping deliveries wins. D2 is the coverage
window and D4 the floor check. No vendor data anywhere; filenames and folder
names are the real shapes, the contents are synthetic.
"""

from __future__ import annotations

import re
import zipfile
from datetime import date, datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from sbh_data_bank.curate.delivery import (
    AsOfUnavailableError,
    as_of_floor_violation,
    filename_window,
    folder_date,
    resolve_as_of,
    resolve_coverage,
    xlsx_created,
    xlsx_modified,
)
from sbh_data_bank.registry.spec import AsOfSource, CoverageSource, SourceSpec


def _spec(**overrides) -> SourceSpec:
    base = {
        "key": "t.s",
        "name": "n",
        "vendor": "v",
        "natural_key": ["id"],
        "variants": [
            {
                "name": "v",
                "detect": {"require_all": ["ID"]},
                "columns": [
                    {"target": "id", "sources": ["ID"]},
                    {
                        "target": "event",
                        "sources": ["E"],
                        "type": "date",
                        "date_formats": ["%Y-%m-%d"],
                    },
                ],
            }
        ],
        "event_date": "event",
    }
    return SourceSpec.model_validate({**base, **overrides})


def _workbook(path: Path, *, created: datetime | None, modified: datetime | None) -> Path:
    """An xlsx whose docProps/core.xml carries the given stamps.

    openpyxl honours ``created`` but stamps ``modified`` with now on save, so
    the modified element is rewritten inside the zip afterwards, the way a
    vendor portal would have written it in the first place.
    """
    wb = Workbook()
    wb.active["A1"] = "ID"
    wb.properties.created = created
    wb.save(path)
    if modified is not None:
        members: dict[str, bytes] = {}
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                members[name] = z.read(name)
        core = members["docProps/core.xml"].decode("utf-8")
        core = re.sub(
            r"<dcterms:modified([^>]*)>[^<]*</dcterms:modified>",
            rf"<dcterms:modified\1>{modified:%Y-%m-%dT%H:%M:%S}Z</dcterms:modified>",
            core,
        )
        members["docProps/core.xml"] = core.encode("utf-8")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in members.items():
                z.writestr(name, data)
    return path


class TestFilenameWindow:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("McKesson_Purchasing Detail - Invoice Level 01012026-04052026.csv",
             (date(2026, 1, 1), date(2026, 4, 5))),
            ("McKesson Purchasing Detail-Invoice Level Jan012025-Feb042026.csv",
             (date(2025, 1, 1), date(2026, 2, 4))),
            ("ABC_Purchases Report-Invoice Detail Jan012025_Feb042026.csv",
             (date(2025, 1, 1), date(2026, 2, 4))),
            ("Master COGS List as of 03062026.xlsx", (None, date(2026, 3, 6))),
            ("Monthly Payments 2026-07 upto 07212026.csv", (None, date(2026, 7, 21))),
            ("Monthly Payments 2025-06.csv", (date(2025, 6, 1), date(2025, 6, 30))),
            ("Athena_Patient Eligibility.csv", (None, None)),
        ],
    )  # fmt: skip
    def test_real_filename_shapes(self, name, expected):
        assert filename_window(name) == expected

    def test_digits_that_are_not_a_date_are_not_a_window(self):
        assert filename_window("report 13452026-99992026.csv") == (None, None)


class TestFolderDate:
    def test_an_iso_dated_folder(self, tmp_path):
        path = tmp_path / "2026-10-18" / "1 Wholesalers" / "x.csv"
        assert folder_date(path) == date(2026, 10, 18)

    def test_a_folder_ending_in_mmddyyyy(self, tmp_path):
        path = tmp_path / "Sagebrush ETL files 03062026" / "1 Wholesalers" / "x.csv"
        assert folder_date(path) == date(2026, 3, 6)

    def test_a_date_range_is_not_a_date(self, tmp_path):
        path = tmp_path / "Jan012026 - Apr052026" / "x.csv"
        assert folder_date(path, root=tmp_path) is None

    def test_the_nearest_dated_folder_wins(self, tmp_path):
        path = tmp_path / "2026-10-11" / "2026-10-18" / "x.csv"
        assert folder_date(path) == date(2026, 10, 18)

    def test_the_search_stops_at_the_root(self, tmp_path):
        root = tmp_path / "2026-10-18"
        path = root / "1 Wholesalers" / "x.csv"
        assert folder_date(path, root=root) is None
        assert folder_date(path) == date(2026, 10, 18)


class TestResolveAsOf:
    def test_a_dated_folder_beats_everything(self, tmp_path):
        """D5: going forward the folder date is the as-of for every file,
        even one whose name says otherwise and whose spec says filename."""
        folder = tmp_path / "2026-10-18"
        folder.mkdir()
        path = folder / "Report 01012026-04052026.csv"
        path.write_text("ID\n", encoding="utf-8")
        found = resolve_as_of(path, _spec(as_of_source="filename"), root=tmp_path)
        assert (found.value, found.source) == (date(2026, 10, 18), AsOfSource.FOLDER)

    def test_the_xlsx_stamp_beats_the_filename(self, tmp_path):
        """ABC's file named through April 5 was created April 30: the stamp
        is the late-download case it exists for."""
        path = _workbook(
            tmp_path / "ABC 01012026-04052026.xlsx",
            created=datetime(2026, 4, 30, 9, 0),
            modified=datetime(2026, 5, 1, 9, 0),
        )
        found = resolve_as_of(path, _spec(), root=tmp_path)
        assert (found.value, found.source) == (date(2026, 4, 30), AsOfSource.XLSX_CREATED)

    def test_a_csv_falls_through_to_the_filename(self, tmp_path):
        path = tmp_path / "Report 01012026-04052026.csv"
        path.write_text("ID\n", encoding="utf-8")
        found = resolve_as_of(path, _spec(), root=tmp_path)
        assert (found.value, found.source) == (date(2026, 4, 5), AsOfSource.FILENAME)

    def test_the_operator_date_is_last(self, tmp_path):
        path = tmp_path / "Report.csv"
        path.write_text("ID\n", encoding="utf-8")
        found = resolve_as_of(path, _spec(), operator_date=date(2026, 6, 10), root=tmp_path)
        assert (found.value, found.source) == (date(2026, 6, 10), AsOfSource.OPERATOR)

    def test_nothing_is_refused_not_guessed(self, tmp_path):
        path = tmp_path / "Report.csv"
        path.write_text("ID\n", encoding="utf-8")
        with pytest.raises(AsOfUnavailableError, match="no as-of date"):
            resolve_as_of(path, _spec(), root=tmp_path)

    def test_a_spec_override_skips_the_stamp(self, tmp_path):
        """ABC: the xlsx stamp is an Excel Online re-save; use the filename."""
        path = _workbook(
            tmp_path / "ABC 01012026-04052026.xlsx",
            created=datetime(2026, 4, 30),
            modified=datetime(2026, 4, 30),
        )
        found = resolve_as_of(path, _spec(as_of_source="filename"), root=tmp_path)
        assert (found.value, found.source) == (date(2026, 4, 5), AsOfSource.FILENAME)

    def test_a_modified_stamp_override(self, tmp_path):
        """Master COGS List: a hand-edited register whose creation stamp
        never moves."""
        path = _workbook(
            tmp_path / "Master COGS List.xlsx",
            created=datetime(2024, 1, 1),
            modified=datetime(2026, 3, 6),
        )
        found = resolve_as_of(path, _spec(as_of_source="xlsx_modified"), root=tmp_path)
        assert (found.value, found.source) == (date(2026, 3, 6), AsOfSource.XLSX_MODIFIED)

    def test_an_override_that_yields_nothing_does_not_fall_through(self, tmp_path):
        path = tmp_path / "Report 01012026-04052026.csv"
        path.write_text("ID\n", encoding="utf-8")
        with pytest.raises(AsOfUnavailableError, match="takes as-of from operator"):
            resolve_as_of(path, _spec(as_of_source="operator"), root=tmp_path)

    def test_a_zip_member_is_named_by_the_caller(self, tmp_path):
        archive = tmp_path / "Monthly Payments.zip"
        archive.write_bytes(b"")
        found = resolve_as_of(
            archive, _spec(), filename="Monthly Payments 2025-06.csv", root=tmp_path
        )
        assert found.value == date(2025, 6, 30)


class TestWorkbookStamps:
    def test_both_stamps_are_read(self, tmp_path):
        path = _workbook(
            tmp_path / "w.xlsx", created=datetime(2026, 1, 2), modified=datetime(2026, 3, 4)
        )
        assert xlsx_created(path) == date(2026, 1, 2)
        assert xlsx_modified(path) == date(2026, 3, 4)

    def test_a_csv_has_no_stamp(self, tmp_path):
        path = tmp_path / "w.csv"
        path.write_text("ID\n", encoding="utf-8")
        assert xlsx_created(path) is None


class TestResolveCoverage:
    def test_from_the_filename(self):
        cov = resolve_coverage(_spec(), filename="R 01012026-04052026.csv", as_of=date(2026, 4, 5))
        assert (cov.start, cov.end, cov.source) == (
            date(2026, 1, 1),
            date(2026, 4, 5),
            CoverageSource.FILENAME,
        )
        assert cov.covers(date(2026, 4, 5)) and not cov.covers(date(2026, 4, 6))

    def test_an_open_start_covers_everything_before_the_end(self):
        cov = resolve_coverage(_spec(), filename="R as of 03062026.csv", as_of=date(2026, 3, 6))
        assert cov.start is None
        assert cov.covers(date(2019, 1, 1))

    def test_unknown_when_the_name_has_no_window(self):
        assert resolve_coverage(_spec(), filename="R.csv", as_of=date(2026, 3, 6)) is None

    def test_from_the_data(self):
        spec = _spec(coverage="data")
        dates = [date(2026, 3, 9), None, datetime(2026, 1, 15, 8, 0), date(2026, 2, 1)]
        cov = resolve_coverage(spec, filename="R.csv", as_of=date(2026, 4, 1), coverage_dates=dates)
        assert (cov.start, cov.end) == (date(2026, 1, 15), date(2026, 3, 9))

    def test_from_the_data_with_no_rows_is_unknown(self):
        spec = _spec(coverage="data")
        assert resolve_coverage(spec, filename="R.csv", as_of=date(2026, 4, 1)) is None

    def test_a_snapshot_covers_everything_up_to_the_as_of(self):
        spec = _spec(event_date="as_of", coverage="as_of")
        cov = resolve_coverage(spec, filename="Eligibility.csv", as_of=date(2026, 2, 4))
        assert (cov.start, cov.end) == (None, date(2026, 2, 4))

    def test_offsets_apply_after_the_window(self):
        """Cardinal excludes the extraction day; WeInfuse includes the day
        before the filename start."""
        spec = _spec(coverage_start_offset_days=-1, coverage_end_offset_days=-1)
        cov = resolve_coverage(spec, filename="R 01012026-04052026.csv", as_of=date(2026, 4, 5))
        assert (cov.start, cov.end) == (date(2025, 12, 31), date(2026, 4, 4))

    def test_an_inverted_window_is_unknown(self):
        spec = _spec(coverage_end_offset_days=-10)
        cov = resolve_coverage(spec, filename="R 04012026-04052026.csv", as_of=date(2026, 4, 5))
        assert cov is None


class TestAsOfFloor:
    def test_data_up_to_the_as_of_is_fine(self):
        assert as_of_floor_violation(date(2026, 4, 5), [date(2026, 4, 5), None]) is None

    def test_next_day_bookings_are_tolerated(self):
        """Athena and Curve carry rows for the day after the pull (D4)."""
        assert as_of_floor_violation(date(2026, 4, 5), [date(2026, 4, 6)]) is None

    def test_two_days_past_is_a_violation(self):
        message = as_of_floor_violation(date(2026, 4, 5), [date(2026, 4, 7)])
        assert "precedes the latest event date 2026-04-07 by 2 days" in message

    def test_timestamps_compare_by_their_day(self):
        assert as_of_floor_violation(date(2026, 4, 5), [datetime(2026, 4, 6, 23, 59)]) is None

    def test_no_dates_means_nothing_to_check(self):
        assert as_of_floor_violation(date(2026, 4, 5), []) is None
