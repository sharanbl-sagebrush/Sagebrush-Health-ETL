"""Effective dating - the mechanism that makes a business rule a row.

Every rule in this platform that can change over time is stored as a series of
versions, each valid over a date interval.  Reconciliation resolves rules
*as of the date of the event being reconciled*, never as of today.  That is
what stops a mapping change made in March from silently rewriting January's
answer.

Intervals are half-open: ``[valid_from, valid_to)``.  The start date is
included, the end date is not.  Half-open intervals compose without gaps or
overlaps and make "which version covers this day" a single unambiguous
question, which closed intervals do not.

The cost of that choice is that it does not match how people speak.  Nobody
says "Southington's 340B participation ended on the 18th"; they say it ended
*on the 17th*, meaning the 17th still counts.  Writing ``valid_to=2025-07-17``
when you mean that excludes the 17th and produces an off-by-one day that in
this domain is a compliance finding, not a rounding error.  So config is
authored through :meth:`DateRange.inclusive`, which takes the last day that
counts and does the conversion:

    DateRange.inclusive(date(2024, 1, 1), date(2025, 7, 17))
    # -> [2024-01-01, 2025-07-18)  - the 17th is covered, the 18th is not

Gaps are legal.  A program that is suspended and later resumed is two
versions with a hole between them, and :meth:`TemporalSet.at` correctly
returns ``None`` inside the hole rather than extending the earlier version.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Generic, Protocol, TypeVar

__all__ = ["DateRange", "TemporalSet", "Versioned", "OverlappingVersionsError"]

_ONE_DAY = timedelta(days=1)


class OverlappingVersionsError(ValueError):
    """Two versions of the same rule claim the same day.

    Always a configuration error.  Resolution would have to pick arbitrarily
    between them, so it refuses instead.
    """


@dataclass(frozen=True, slots=True)
class DateRange:
    """A half-open date interval ``[start, end)``.

    ``end`` of ``None`` means open-ended - still in force.
    """

    start: date
    end: date | None = None

    def __post_init__(self) -> None:
        if self.end is not None and self.end <= self.start:
            raise ValueError(
                f"empty or reversed interval: [{self.start}, {self.end}). "
                f"If you meant a single day, use DateRange.inclusive({self.start}, {self.start})."
            )

    @classmethod
    def inclusive(cls, first_day: date, last_day: date | None = None) -> DateRange:
        """Build a range from the first and last days that *count*.

        This is the constructor configuration should use, because it matches
        how the rule was stated by the person who owns it.

        >>> DateRange.inclusive(date(2024, 1, 1), date(2025, 7, 17))
        DateRange(start=datetime.date(2024, 1, 1), end=datetime.date(2025, 7, 18))
        """
        return cls(first_day, None if last_day is None else last_day + _ONE_DAY)

    @property
    def last_day(self) -> date | None:
        """The final day covered, or ``None`` if open-ended.

        The inverse of :meth:`inclusive`; use it when displaying a range to a
        human, so the UI shows the 17th rather than the 18th.
        """
        return None if self.end is None else self.end - _ONE_DAY

    @property
    def is_open_ended(self) -> bool:
        return self.end is None

    def covers(self, day: date) -> bool:
        """Whether ``day`` falls in this interval.

        >>> r = DateRange.inclusive(date(2024, 1, 1), date(2025, 7, 17))
        >>> r.covers(date(2025, 7, 17)), r.covers(date(2025, 7, 18))
        (True, False)
        """
        return self.start <= day and (self.end is None or day < self.end)

    def overlaps(self, other: DateRange) -> bool:
        """Whether two intervals share at least one day."""
        starts_before_other_ends = other.end is None or self.start < other.end
        other_starts_before_self_ends = self.end is None or other.start < self.end
        return starts_before_other_ends and other_starts_before_self_ends

    def __str__(self) -> str:
        return f"[{self.start} .. {self.last_day or 'open'}]"


class Versioned(Protocol):
    """Anything carrying the interval over which it is in force."""

    @property
    def effective(self) -> DateRange: ...


V = TypeVar("V", bound=Versioned)


class TemporalSet(Generic[V]):
    """All versions of one rule, resolvable by date.

    Construction validates that no two versions claim the same day, so an
    overlapping configuration fails at load time rather than producing a
    silently arbitrary answer during a reconciliation run months later.

    >>> from dataclasses import dataclass
    >>> @dataclass(frozen=True)
    ... class Enrollment:
    ...     site: str
    ...     effective: DateRange
    >>> ct = TemporalSet([
    ...     Enrollment("CT", DateRange.inclusive(date(2024, 1, 1), date(2025, 7, 17))),
    ... ])
    >>> ct.at(date(2025, 7, 17)).site
    'CT'
    >>> ct.at(date(2025, 7, 18)) is None
    True
    """

    __slots__ = ("_versions",)

    def __init__(self, versions: Iterable[V]) -> None:
        ordered = sorted(versions, key=lambda v: v.effective.start)
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if earlier.effective.overlaps(later.effective):
                raise OverlappingVersionsError(
                    f"versions overlap: {earlier.effective} and {later.effective}"
                )
        self._versions = tuple(ordered)

    def at(self, day: date) -> V | None:
        """The version in force on ``day``, or ``None`` if none is.

        ``None`` is a real answer, not a failure - it means the rule did not
        apply that day.  Callers must decide what that means for them rather
        than falling back to the nearest version.
        """
        for version in self._versions:
            if version.effective.covers(day):
                return version
        return None

    def require_at(self, day: date, what: str = "rule") -> V:
        """:meth:`at`, but raises when nothing is in force.

        For rules where a gap is a configuration error rather than a
        meaningful state.
        """
        found = self.at(day)
        if found is None:
            raise LookupError(
                f"no {what} in force on {day}; "
                f"known intervals: {', '.join(str(v.effective) for v in self._versions) or 'none'}"
            )
        return found

    def history(self) -> tuple[V, ...]:
        """Every version, oldest first."""
        return self._versions

    def covered_days_bounds(self) -> tuple[date, date | None] | None:
        """Earliest start and latest end across all versions, or ``None`` if empty.

        Note this spans any gaps; it is a bounding box, not a coverage claim.
        """
        if not self._versions:
            return None
        latest_end: date | None = self._versions[0].effective.end
        for version in self._versions:
            if version.effective.end is None:
                latest_end = None
                break
            if latest_end is not None and version.effective.end > latest_end:
                latest_end = version.effective.end
        return self._versions[0].effective.start, latest_end

    def __iter__(self) -> Iterator[V]:
        return iter(self._versions)

    def __len__(self) -> int:
        return len(self._versions)

    def __bool__(self) -> bool:
        return bool(self._versions)

    def __repr__(self) -> str:
        return f"TemporalSet({len(self._versions)} versions)"
