"""Tests for effective dating - ``sbh_data_bank.core.temporal``.

"A business rule is a row, not a line of code": a rule such as "Southington
stopped 340B on 2025-07-17" is stored as a dated version and resolved as-of
the event date. ``DateRange`` is the interval type and ``TemporalSet`` holds
the non-overlapping versions of one rule.

The Southington case is used throughout because it is the real rule the
platform has to get right, and because the inclusive/exclusive boundary is
exactly where a compliance error would hide.

How to read this file:

- TestDateRange    one interval: inclusive start, exclusive end, the
                   ``inclusive()`` constructor that takes a human "last day"
- TestOverlap      when two intervals collide
- TestTemporalSet  resolving the version in force on a date, gaps, overlaps
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
    """A stand-in for a program-enrollment row: anything with an ``effective``
    DateRange can live in a TemporalSet."""

    site: str
    effective: DateRange


class TestDateRange:
    """One interval. Start is inclusive, ``end`` is exclusive, and
    ``inclusive()`` converts the last day a human writes into that form."""

    def test_inclusive_converts_last_day_to_exclusive_end(self):
        """The human writes 17 July; the arithmetic stores 18 July as the end."""
        r = DateRange.inclusive(date(2024, 1, 1), SOUTHINGTON_LAST_DAY)
        assert r.end == date(2025, 7, 18)
        assert r.last_day == SOUTHINGTON_LAST_DAY

    def test_the_stated_last_day_is_covered(self):
        """The 17th is in; the 18th is out."""
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
        """The first day of a range is inside it."""
        assert DateRange(date(2024, 1, 1)).covers(date(2024, 1, 1))

    def test_open_ended_covers_the_far_future(self):
        """No end means "until further notice": any later date is covered."""
        r = DateRange(date(2024, 1, 1))
        assert r.is_open_ended
        assert r.last_day is None
        assert r.covers(date(2099, 12, 31))

    def test_single_day_range(self):
        """A one-day rule is representable and covers exactly that day."""
        r = DateRange.inclusive(date(2025, 7, 17), date(2025, 7, 17))
        assert r.covers(date(2025, 7, 17))
        assert not r.covers(date(2025, 7, 16))
        assert not r.covers(date(2025, 7, 18))

    def test_before_start_is_not_covered(self):
        """The day before the start is outside."""
        assert not DateRange(date(2024, 1, 1)).covers(date(2023, 12, 31))

    @pytest.mark.parametrize("end", [date(2024, 1, 1), date(2023, 12, 31)])
    def test_empty_or_reversed_interval_is_rejected(self, end):
        """A range that ends on or before its start is a data-entry error,
        not an empty set, so it fails at construction."""
        with pytest.raises(ValueError, match="empty or reversed"):
            DateRange(date(2024, 1, 1), end)

    def test_str_shows_the_inclusive_last_day(self):
        """The printed form uses the human convention, so a log line reads the
        way the rule was written, not the way it is stored."""
        r = DateRange.inclusive(date(2024, 1, 1), SOUTHINGTON_LAST_DAY)
        assert str(r) == "[2024-01-01 .. 2025-07-17]"
        assert str(DateRange(date(2024, 1, 1))) == "[2024-01-01 .. open]"


class TestOverlap:
    """When two intervals collide. Overlap is what a TemporalSet forbids."""

    def test_adjacent_ranges_do_not_overlap(self):
        """Ends 30 June, starts 1 July: back to back, not overlapping. This is
        what the exclusive end buys."""
        a = DateRange.inclusive(date(2024, 1, 1), date(2024, 6, 30))
        b = DateRange(date(2024, 7, 1))
        assert not a.overlaps(b)
        assert not b.overlaps(a)

    def test_sharing_one_day_is_an_overlap(self):
        """One shared day is enough: two rules would be in force on 1 July."""
        a = DateRange.inclusive(date(2024, 1, 1), date(2024, 7, 1))
        b = DateRange(date(2024, 7, 1))
        assert a.overlaps(b)

    def test_open_ended_overlaps_everything_after_its_start(self):
        """An open range reaches any later start."""
        assert DateRange(date(2024, 1, 1)).overlaps(DateRange(date(2030, 1, 1)))

    def test_two_open_ended_ranges_always_overlap(self):
        """Two "until further notice" rules cannot both be in force."""
        assert DateRange(date(2024, 1, 1)).overlaps(DateRange(date(2020, 1, 1)))


class TestTemporalSet:
    """Versions of one rule, resolved by date. ``at()`` returns the version in
    force or None; ``require_at()`` raises instead of returning None."""

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
        """The real rule end to end: 16 July in, 17 July in, 18 July out, and
        still out six months later."""
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
        """A date before the first version has no rule in force."""
        s = TemporalSet([Enrollment("CT", DateRange(date(2024, 1, 1)))])
        assert s.at(date(2023, 1, 1)) is None

    def test_overlapping_versions_are_rejected_at_construction(self):
        """Two versions in force on the same day is a contradiction. It is
        caught when the set is built, not when somebody queries it."""
        with pytest.raises(OverlappingVersionsError):
            TemporalSet(
                [
                    Enrollment("CT", DateRange.inclusive(date(2024, 1, 1), date(2024, 12, 31))),
                    Enrollment("CT", DateRange(date(2024, 6, 1))),
                ]
            )

    def test_versions_may_be_supplied_out_of_order(self):
        """Rows come back from a database in no particular order; the set
        sorts them by start."""
        s = TemporalSet(
            [
                Enrollment("later", DateRange(date(2025, 1, 1))),
                Enrollment("earlier", DateRange.inclusive(date(2024, 1, 1), date(2024, 12, 31))),
            ]
        )
        assert [v.site for v in s.history()] == ["earlier", "later"]
        assert s.at(date(2024, 5, 1)).site == "earlier"

    def test_require_at_raises_with_a_useful_message(self):
        """For callers that cannot proceed without a rule: the error names
        what was missing and on which day."""
        s = TemporalSet(
            [Enrollment("CT", DateRange.inclusive(date(2024, 1, 1), SOUTHINGTON_LAST_DAY))]
        )
        with pytest.raises(LookupError, match="no enrollment in force on 2025-07-18"):
            s.require_at(date(2025, 7, 18), what="enrollment")

    def test_require_at_returns_the_version_when_present(self):
        """The happy path of require_at is the same as at()."""
        s = TemporalSet([Enrollment("CT", DateRange(date(2024, 1, 1)))])
        assert s.require_at(date(2024, 5, 1)).site == "CT"

    def test_empty_set(self):
        """An empty set is falsy, has no bounds, and resolves nothing."""
        s: TemporalSet[Enrollment] = TemporalSet([])
        assert not s
        assert len(s) == 0
        assert s.at(date(2024, 1, 1)) is None
        assert s.covered_days_bounds() is None

    def test_bounds_span_gaps(self):
        """covered_days_bounds reports the outer envelope (exclusive end) and
        ignores any gap inside it."""
        s = TemporalSet(
            [
                Enrollment("a", DateRange.inclusive(date(2024, 1, 1), date(2024, 6, 30))),
                Enrollment("b", DateRange.inclusive(date(2025, 1, 1), date(2025, 12, 31))),
            ]
        )
        assert s.covered_days_bounds() == (date(2024, 1, 1), date(2026, 1, 1))

    def test_bounds_are_open_when_any_version_is_open(self):
        """One open-ended version makes the whole envelope open-ended."""
        s = TemporalSet(
            [
                Enrollment("a", DateRange.inclusive(date(2024, 1, 1), date(2024, 6, 30))),
                Enrollment("b", DateRange(date(2025, 1, 1))),
            ]
        )
        assert s.covered_days_bounds() == (date(2024, 1, 1), None)

    def test_iteration_and_len(self):
        """Iterating yields versions in start order, same as history()."""
        s = TemporalSet(
            [
                Enrollment("a", DateRange.inclusive(date(2024, 1, 1), date(2024, 6, 30))),
                Enrollment("b", DateRange(date(2025, 1, 1))),
            ]
        )
        assert len(s) == 2
        assert [v.site for v in s] == ["a", "b"]
