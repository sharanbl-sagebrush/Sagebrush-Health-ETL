"""Tests for effective dating.

The Southington case is used throughout because it is the real rule the
platform has to get right, and because the inclusive/exclusive boundary is
exactly where a compliance error would hide.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from sbh_data_bank.core.temporal import (
    DateRange,
    OverlappingVersionsError,
    TemporalSet,
)

# "Southington stopped 340B on 2025-07-17" - the 17th still counts.
SOUTHINGTON_LAST_DAY = date(2025, 7, 17)


@dataclass(frozen=True)
class Enrollment:
    site: str
    effective: DateRange


class TestDateRange:
    def test_inclusive_converts_last_day_to_exclusive_end(self):
        r = DateRange.inclusive(date(2024, 1, 1), SOUTHINGTON_LAST_DAY)
        assert r.end == date(2025, 7, 18)
        assert r.last_day == SOUTHINGTON_LAST_DAY

    def test_the_stated_last_day_is_covered(self):
        r = DateRange.inclusive(date(2024, 1, 1), SOUTHINGTON_LAST_DAY)
        assert r.covers(SOUTHINGTON_LAST_DAY)
        assert not r.covers(date(2025, 7, 18))

    def test_writing_the_raw_end_date_would_have_excluded_the_last_day(self):
        """The off-by-one this API exists to prevent."""
        naive = DateRange(date(2024, 1, 1), SOUTHINGTON_LAST_DAY)
        assert not naive.covers(SOUTHINGTON_LAST_DAY)
        assert DateRange.inclusive(date(2024, 1, 1), SOUTHINGTON_LAST_DAY).covers(
            SOUTHINGTON_LAST_DAY
        )

    def test_start_is_covered(self):
        assert DateRange(date(2024, 1, 1)).covers(date(2024, 1, 1))

    def test_open_ended_covers_the_far_future(self):
        r = DateRange(date(2024, 1, 1))
        assert r.is_open_ended
        assert r.last_day is None
        assert r.covers(date(2099, 12, 31))

    def test_single_day_range(self):
        r = DateRange.inclusive(date(2025, 7, 17), date(2025, 7, 17))
        assert r.covers(date(2025, 7, 17))
        assert not r.covers(date(2025, 7, 16))
        assert not r.covers(date(2025, 7, 18))

    def test_before_start_is_not_covered(self):
        assert not DateRange(date(2024, 1, 1)).covers(date(2023, 12, 31))

    @pytest.mark.parametrize("end", [date(2024, 1, 1), date(2023, 12, 31)])
    def test_empty_or_reversed_interval_is_rejected(self, end):
        with pytest.raises(ValueError, match="empty or reversed"):
            DateRange(date(2024, 1, 1), end)

    def test_str_shows_the_inclusive_last_day(self):
        r = DateRange.inclusive(date(2024, 1, 1), SOUTHINGTON_LAST_DAY)
        assert str(r) == "[2024-01-01 .. 2025-07-17]"
        assert str(DateRange(date(2024, 1, 1))) == "[2024-01-01 .. open]"


class TestOverlap:
    def test_adjacent_ranges_do_not_overlap(self):
        a = DateRange.inclusive(date(2024, 1, 1), date(2024, 6, 30))
        b = DateRange(date(2024, 7, 1))
        assert not a.overlaps(b)
        assert not b.overlaps(a)

    def test_sharing_one_day_is_an_overlap(self):
        a = DateRange.inclusive(date(2024, 1, 1), date(2024, 7, 1))
        b = DateRange(date(2024, 7, 1))
        assert a.overlaps(b)

    def test_open_ended_overlaps_everything_after_its_start(self):
        assert DateRange(date(2024, 1, 1)).overlaps(DateRange(date(2030, 1, 1)))

    def test_two_open_ended_ranges_always_overlap(self):
        assert DateRange(date(2024, 1, 1)).overlaps(DateRange(date(2020, 1, 1)))


class TestTemporalSet:
    def test_resolves_the_version_in_force(self):
        """A TemporalSet holds versions of ONE rule, so it resolves by date alone.

        Two sites are two sets, not two versions - putting them in one set is
        an overlap error, which is asserted separately below.
        """
        s = TemporalSet(
            [
                Enrollment("v1", DateRange.inclusive(date(2024, 1, 1), date(2024, 12, 31))),
                Enrollment("v2", DateRange(date(2025, 1, 1))),
            ]
        )
        assert s.at(date(2024, 6, 1)).site == "v1"
        assert s.at(date(2025, 6, 1)).site == "v2"

    def test_open_ended_version_sorts_without_error(self):
        """Regression: comparing an open-ended range used to raise TypeError."""
        s = TemporalSet(
            [
                Enrollment("open", DateRange(date(2025, 1, 1))),
                Enrollment("closed", DateRange.inclusive(date(2024, 1, 1), date(2024, 12, 31))),
            ]
        )
        assert [v.site for v in s.history()] == ["closed", "open"]

    def test_nested_intervals_are_detected_as_overlapping(self):
        """Sorting by start relies on adjacent-pair checking catching nesting."""
        with pytest.raises(OverlappingVersionsError):
            TemporalSet(
                [
                    Enrollment("outer", DateRange.inclusive(date(2024, 1, 1), date(2024, 12, 31))),
                    Enrollment("inner", DateRange.inclusive(date(2024, 3, 1), date(2024, 4, 1))),
                    Enrollment("also", DateRange.inclusive(date(2024, 6, 1), date(2024, 7, 1))),
                ]
            )

    def test_southington_is_enrolled_through_the_seventeenth_and_not_after(self):
        s = TemporalSet(
            [Enrollment("CT", DateRange.inclusive(date(2024, 1, 1), SOUTHINGTON_LAST_DAY))]
        )
        assert s.at(date(2025, 7, 16)).site == "CT"
        assert s.at(date(2025, 7, 17)).site == "CT"
        assert s.at(date(2025, 7, 18)) is None
        assert s.at(date(2026, 1, 1)) is None

    def test_a_gap_returns_none_rather_than_extending_the_earlier_version(self):
        """A suspended-then-resumed program must not appear continuous."""
        s = TemporalSet(
            [
                Enrollment("CT", DateRange.inclusive(date(2024, 1, 1), date(2024, 6, 30))),
                Enrollment("CT", DateRange(date(2025, 1, 1))),
            ]
        )
        assert s.at(date(2024, 6, 30)) is not None
        assert s.at(date(2024, 9, 1)) is None
        assert s.at(date(2025, 1, 1)) is not None

    def test_before_any_version_returns_none(self):
        s = TemporalSet([Enrollment("CT", DateRange(date(2024, 1, 1)))])
        assert s.at(date(2023, 1, 1)) is None

    def test_overlapping_versions_are_rejected_at_construction(self):
        with pytest.raises(OverlappingVersionsError):
            TemporalSet(
                [
                    Enrollment("CT", DateRange.inclusive(date(2024, 1, 1), date(2024, 12, 31))),
                    Enrollment("CT", DateRange(date(2024, 6, 1))),
                ]
            )

    def test_versions_may_be_supplied_out_of_order(self):
        s = TemporalSet(
            [
                Enrollment("later", DateRange(date(2025, 1, 1))),
                Enrollment("earlier", DateRange.inclusive(date(2024, 1, 1), date(2024, 12, 31))),
            ]
        )
        assert [v.site for v in s.history()] == ["earlier", "later"]
        assert s.at(date(2024, 5, 1)).site == "earlier"

    def test_require_at_raises_with_a_useful_message(self):
        s = TemporalSet(
            [Enrollment("CT", DateRange.inclusive(date(2024, 1, 1), SOUTHINGTON_LAST_DAY))]
        )
        with pytest.raises(LookupError, match="no enrollment in force on 2025-07-18"):
            s.require_at(date(2025, 7, 18), what="enrollment")

    def test_require_at_returns_the_version_when_present(self):
        s = TemporalSet([Enrollment("CT", DateRange(date(2024, 1, 1)))])
        assert s.require_at(date(2024, 5, 1)).site == "CT"

    def test_empty_set(self):
        s: TemporalSet[Enrollment] = TemporalSet([])
        assert not s
        assert len(s) == 0
        assert s.at(date(2024, 1, 1)) is None
        assert s.covered_days_bounds() is None

    def test_bounds_span_gaps(self):
        s = TemporalSet(
            [
                Enrollment("a", DateRange.inclusive(date(2024, 1, 1), date(2024, 6, 30))),
                Enrollment("b", DateRange.inclusive(date(2025, 1, 1), date(2025, 12, 31))),
            ]
        )
        assert s.covered_days_bounds() == (date(2024, 1, 1), date(2026, 1, 1))

    def test_bounds_are_open_when_any_version_is_open(self):
        s = TemporalSet(
            [
                Enrollment("a", DateRange.inclusive(date(2024, 1, 1), date(2024, 6, 30))),
                Enrollment("b", DateRange(date(2025, 1, 1))),
            ]
        )
        assert s.covered_days_bounds() == (date(2024, 1, 1), None)

    def test_iteration_and_len(self):
        s = TemporalSet(
            [
                Enrollment("a", DateRange.inclusive(date(2024, 1, 1), date(2024, 6, 30))),
                Enrollment("b", DateRange(date(2025, 1, 1))),
            ]
        )
        assert len(s) == 2
        assert [v.site for v in s] == ["a", "b"]
