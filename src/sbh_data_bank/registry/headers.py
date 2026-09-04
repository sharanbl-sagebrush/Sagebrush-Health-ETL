"""Matching vendor column headers to spec fields.

Vendor headers are written by humans for humans and are not stable. Observed
in the Sagebrush extracts:

``Days` Supply``
    Wellpartner ships a backtick in the header. The previous parser matched it
    by reproducing the typo exactly, so the field would have silently gone null
    the day the vendor fixed their own header.
``LOCATION_NAME1`` / ``LOCATION_NAME``
    Allscripts renamed a column and the previous parser carried a bespoke
    ``or`` fallback for that one case.
``Purchase Dollars (Exclude Taxes, Fees & Surcharges)``
    Cardinal. Punctuation-heavy, and a candidate for the vendor reflowing the
    parenthetical at any time.
``10 Digit NDC (Current)`` vs ``NDC (Current)``
    McKesson. Two columns whose normalized forms must stay distinct.

So headers are matched on a normalized form rather than byte-for-byte, and a
field declares a list of accepted names rather than one. Normalization folds
case, punctuation and whitespace - the things vendors change without meaning
anything by it - and nothing else. It deliberately does not stem, fuzzy-match
or edit-distance: "Ordered Quantity" and "Order Quantity" are different columns
in the McKesson export, and a matcher clever enough to unify them would be
wrong in a way nobody would notice for months.

Ambiguity is always an error. If two vendor columns normalize to the same
thing, or one spec field matches two columns, the load stops rather than
picking one.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

__all__ = [
    "AmbiguousHeaderError",
    "HeaderIndex",
    "HeaderMatch",
    "normalize_header",
]

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

#: pandas names an unnamed column "Unnamed: 0"; SSRS exports emit "Textbox42".
#: Both are export artifacts rather than data, and both appear in these feeds.
_JUNK_PATTERNS = (
    re.compile(r"^unnamed\s*\d*$"),
    re.compile(r"^textbox\s*\d*$"),
    re.compile(r"^column\s*\d+$"),
    re.compile(r"^$"),
)


class AmbiguousHeaderError(ValueError):
    """A header could not be matched to exactly one field, or vice versa.

    Never resolved by guessing. Either the spec needs a more specific accepted
    name, or the vendor has genuinely shipped duplicate columns and someone has
    to decide what that means.
    """


def normalize_header(raw: object) -> str:
    """Fold a vendor header to its comparable form.

    Case, punctuation and runs of whitespace are removed; word boundaries
    become single underscores.

    >>> normalize_header("Days` Supply")
    'days_supply'
    >>> normalize_header("Days Supply")
    'days_supply'
    >>> normalize_header("Purchase Dollars (Exclude Taxes, Fees & Surcharges)")
    'purchase_dollars_exclude_taxes_fees_surcharges'
    >>> normalize_header("  Account #  ")
    'account'

    Distinctions that matter are preserved:

    >>> normalize_header("10 Digit NDC (Current)") == normalize_header("NDC (Current)")
    False
    """
    text = str(raw) if raw is not None else ""
    return _NON_ALNUM.sub("_", text.strip().lower()).strip("_")


def is_junk_header(raw: object) -> bool:
    """Whether a header is an export artifact rather than a real column.

    >>> [is_junk_header(h) for h in ("Unnamed: 0", "Textbox42", "NDC", "")]
    [True, True, False, True]
    """
    normalized = normalize_header(raw).replace("_", " ").strip()
    return any(pattern.match(normalized) for pattern in _JUNK_PATTERNS)


@dataclass(frozen=True, slots=True)
class HeaderMatch:
    """One resolved field-to-column binding."""

    target: str
    """The spec's field name."""

    source_header: str
    """The vendor header as it actually appeared, kept for the audit trail."""

    position: int
    """Zero-based column index in the file."""

    matched_alias: str
    """Which of the field's accepted names matched. Tells you when a source has
    moved onto a newer header, which is worth knowing before the old one is
    dropped entirely."""


class HeaderIndex:
    """The normalized headers of one file, queryable by a spec's accepted names.

    >>> idx = HeaderIndex(["Account #", "NDC", "Days` Supply", "Unnamed: 0"])
    >>> idx.find("supply_days", ["Days Supply", "Day Supply"]).source_header
    'Days` Supply'
    >>> idx.find("missing", ["Nope"]) is None
    True
    >>> sorted(idx.unmatched_headers({1}))
    ['Account #', 'Days` Supply']
    """

    __slots__ = ("_by_normalized", "_headers", "_junk")

    def __init__(self, headers: list[object]) -> None:
        self._headers: list[str] = [("" if h is None else str(h)) for h in headers]
        self._junk: set[int] = {i for i, h in enumerate(self._headers) if is_junk_header(h)}

        by_normalized: dict[str, list[int]] = defaultdict(list)
        for position, header in enumerate(self._headers):
            if position in self._junk:
                continue
            by_normalized[normalize_header(header)].append(position)

        duplicates = {k: v for k, v in by_normalized.items() if len(v) > 1}
        if duplicates:
            detail = "; ".join(
                f"{key!r} at positions {positions} "
                f"({', '.join(repr(self._headers[p]) for p in positions)})"
                for key, positions in sorted(duplicates.items())
            )
            raise AmbiguousHeaderError(
                f"file has columns whose names are indistinguishable after normalization: {detail}"
            )

        self._by_normalized: dict[str, int] = {k: v[0] for k, v in by_normalized.items()}

    def find(self, target: str, accepted_names: list[str]) -> HeaderMatch | None:
        """Resolve one field, or ``None`` when the file has none of its names.

        Accepted names are tried in order, so a spec lists the current header
        first and older ones after it.
        """
        for alias in accepted_names:
            position = self._by_normalized.get(normalize_header(alias))
            if position is not None:
                return HeaderMatch(
                    target=target,
                    source_header=self._headers[position],
                    position=position,
                    matched_alias=alias,
                )
        return None

    def has_all(self, accepted_names: list[str]) -> bool:
        """Whether every one of these headers is present.

        Used by variant detection, which needs a yes/no on a fingerprint rather
        than a binding.
        """
        return all(normalize_header(n) in self._by_normalized for n in accepted_names)

    def has_any(self, accepted_names: list[str]) -> bool:
        return any(normalize_header(n) in self._by_normalized for n in accepted_names)

    def unmatched_headers(self, matched_positions: set[int]) -> set[str]:
        """Real columns the spec did not claim.

        Takes positions rather than field names: a spec's field names and a
        vendor's headers are different namespaces, and comparing across them
        only works by accident when a field happens to be named after its
        column.

        This is the drift signal. A vendor adding a column shows up here, and
        policy decides whether that is a warning or a failure. The previous
        pipeline dropped these silently, which is how a new column could carry
        a corrected value for months with nobody noticing.
        """
        return {
            header
            for position, header in enumerate(self._headers)
            if position not in self._junk and position not in matched_positions
        }

    @property
    def headers(self) -> tuple[str, ...]:
        """Every header as it appeared, junk included, in file order."""
        return tuple(self._headers)

    @property
    def junk_headers(self) -> tuple[str, ...]:
        """Export artifacts that were excluded from matching."""
        return tuple(self._headers[i] for i in sorted(self._junk))

    def fingerprint(self) -> frozenset[str]:
        """The normalized, junk-free header set - used to recognise a variant."""
        return frozenset(self._by_normalized)

    def __len__(self) -> int:
        return len(self._by_normalized)

    def __contains__(self, name: object) -> bool:
        return normalize_header(name) in self._by_normalized
