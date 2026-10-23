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

How this fits
-------------
``ingest.reader`` builds a ``HeaderIndex`` from the header row it finds in a
file, and ``registry.binding`` queries that index once per spec column to
produce the file-to-spec wiring. ``normalize_header`` and ``is_junk_header``
are also used directly by ``scripts/audit_files.py``, ``scripts/scaffold_spec.py``
and the spec generators under ``tools/spec_build``, so a header is folded the
same way whether a spec is being written or a file is being loaded. This module
imports nothing from the rest of the package.
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

# Any run of characters that is not a lowercase letter or digit. Applied after
# lower-casing, so it catches spaces, punctuation, backticks, the "#" in
# "Account #", and the "$" in "Extended Price ($)" alike.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

#: pandas names an unnamed column "Unnamed: 0"; SSRS exports emit "Textbox42".
#: Both are export artifacts rather than data, and both appear in these feeds.
# Matched against the normalized form with underscores turned back into
# spaces, so "Unnamed: 0" (normalized "unnamed_0") is tested as "unnamed 0".
# The empty pattern catches a header cell that was blank in the file. The
# unnamed row-number column at position 0 of both McKesson reports and the
# new-module WeInfuse workbooks (file audit) is excluded by these.
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

    A dollar or percent sign is a word, not punctuation: the pricing
    workbook has ``margin_$`` beside ``margin_%`` (2026-10-23), and
    folding both to ``margin`` made the file unreadable. Spec sources go
    through the same function, so ``Extended Price ($)`` still matches
    itself.

    >>> normalize_header("margin_$"), normalize_header("margin_%")
    ('margin_dollar', 'margin_pct')

    Takes any object because openpyxl hands back whatever the cell held, which
    can be a number, a datetime or ``None``. Never raises.
    """
    # None (an empty workbook cell) becomes the empty string rather than the
    # text "None", which would otherwise look like a real header.
    text = str(raw) if raw is not None else ""
    text = text.replace("$", " dollar ").replace("%", " pct ")
    # Three steps, each one safe because it only removes what vendors change
    # by accident: outer whitespace, letter case, then every non-alphanumeric
    # run collapsed to one underscore. The final strip removes underscores
    # left at either end by leading or trailing punctuation ("Account #").
    # Nothing is added, reordered or abbreviated, so two headers that differ
    # in a letter or digit stay different.
    return _NON_ALNUM.sub("_", text.strip().lower()).strip("_")


def is_junk_header(raw: object) -> bool:
    """Whether a header is an export artifact rather than a real column.

    >>> [is_junk_header(h) for h in ("Unnamed: 0", "Textbox42", "NDC", "")]
    [True, True, False, True]
    """
    # Normalize first so the patterns see one spelling, then put spaces back
    # so "unnamed_0" reads as "unnamed 0" and the optional-space in each
    # pattern applies.
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

    Built once per file from its header row. Raises ``AmbiguousHeaderError``
    at construction when two real columns normalize to the same name, so no
    lookup can ever be answered with a guess.
    """

    # Three pieces of state: every header as shipped (file order), the set of
    # positions judged junk, and the normalized-name-to-position map used by
    # every lookup.
    __slots__ = ("_by_normalized", "_headers", "_junk")

    def __init__(self, headers: list[object]) -> None:
        # Keep the raw text of every header, junk included, so error messages
        # and the audit trail can show what the vendor actually sent.
        self._headers: list[str] = [("" if h is None else str(h)) for h in headers]
        # Junk positions are excluded from matching and from drift. They are
        # export artifacts (row-number columns, SSRS Textbox cells), so a spec
        # should neither claim them nor be told they are "new columns".
        self._junk: set[int] = {i for i, h in enumerate(self._headers) if is_junk_header(h)}

        # Group real positions by normalized name. A list per name, because
        # the next step needs to know when a name maps to more than one.
        by_normalized: dict[str, list[int]] = defaultdict(list)
        for position, header in enumerate(self._headers):
            if position in self._junk:
                continue
            by_normalized[normalize_header(header)].append(position)

        # Two columns that fold to one name cannot be told apart by any
        # accepted-name lookup, so refuse the whole file now. The message
        # shows both the folded name and each original spelling so the
        # operator can see why they collided.
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

        # Every list now has exactly one entry, so flatten to name -> position.
        self._by_normalized: dict[str, int] = {k: v[0] for k, v in by_normalized.items()}

    def find(self, target: str, accepted_names: list[str]) -> HeaderMatch | None:
        """Resolve one field, or ``None`` when the file has none of its names.

        Accepted names are tried in order, so a spec lists the current header
        first and older ones after it.
        """
        # First accepted name that is present wins. Because construction
        # already guaranteed each normalized name maps to one position, the
        # first hit is the only possible hit for that alias. Later aliases are
        # not checked once one matches, which is what makes the order in the
        # spec a preference order.
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
        """Whether at least one of these headers is present. Same normalized
        comparison as ``has_all``."""
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
        # Everything that is neither junk nor claimed by some spec column.
        # Returned as the original header text so the drift report reads the
        # way the vendor wrote it.
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
        # Number of real (non-junk) columns, not the raw header count.
        return len(self._by_normalized)

    def __contains__(self, name: object) -> bool:
        # Lets a detector write ``"NDC" in headers`` and get the normalized
        # comparison. This is what select_variant in registry.binding uses.
        return normalize_header(name) in self._by_normalized
