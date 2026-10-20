"""National Drug Code normalization.

The canonical form used throughout this platform is the 11-digit 5-4-2 NDC
(5-digit labeler, 4-digit product, 2-digit package), which is what payers and
the CMS ASP file use.

Drug packages print a 10-digit NDC in one of three segment configurations:

    4-4-2   pad the labeler    0LLLL-PPPP-KK
    5-3-2   pad the product    LLLLL-0PPP-KK
    5-4-1   pad the package    LLLLL-PPPP-0K

If the source gives hyphens, the configuration is recoverable by counting
segment lengths and the conversion is deterministic.

If the source strips the hyphens, **the configuration is not recoverable from
the digits alone**.  All three expansions are syntactically valid and they
name three different drugs.  The only correct resolution is to expand all
three and look them up in an NDC directory; a unique hit is the answer, and
zero or several hits is an honest AMBIGUOUS/NOT_FOUND outcome that belongs in
quarantine, not a guess.

This is the defect being corrected.  Both the previous SQL
(``RIGHT('00000000000' + REPLACE(ndc,'-',''), 11)``) and the previous Python
(``re.sub(r"[^0-9]", "", s).zfill(11)``) are naive left-pads.  A left-pad
implements the 4-4-2 rule and only the 4-4-2 rule, so it silently produces the
wrong drug for every 5-3-2 and 5-4-1 NDC.  A worked example straight out of the
McKesson Custom export, which carries both forms on the same row:

    10-digit   2568202801
    11-digit   25682002801   vendor-supplied truth, a 5-3-2 NDC
    zfill(11)  02568202801   what the old code produced - a different labeler

Measured against that export (``docs/data-findings.md``, finding 2): the
left-pad was wrong on 55 of 101 distinct NDCs, 54.5%, inventing 55 codes that
do not exist.  This module scores 101/101.

The three paths through :func:`normalize_ndc`, in the order they are tried:

1. Hyphenated input - count the segment widths, pad the deficient segment.
   Deterministic, no directory needed.
2. Eleven digits after stripping punctuation - accept as already canonical.
3. Ten digits, unhyphenated - expand all three ways and ask the directory.
   One hit resolves; zero or many quarantines.

Every outcome is an :class:`NdcResolution` carrying the raw input, the result
(or ``None``), and the :class:`ResolutionMethod` that produced it, so the
warehouse can store ``ndc_raw``, ``ndc11`` and ``ndc_resolution_method`` on
every row and the reasoning is never lost.

How this fits
-------------
Called by ``ingest.reader`` for every column a spec types as ``ndc``; the
reader passes the directory it was given, and ``ingest.load`` threads that
directory through from its caller.  ``NdcDirectory`` is the only thing the
store needs to provide; ``SetNdcDirectory`` is the in-memory stand-in used by
tests and by ``tests/fixtures/ndc_ground_truth_mckesson.json``.  Depends on
nothing else in the package.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

__all__ = [
    "NdcDirectory",
    "NdcResolution",
    "ResolutionMethod",
    "SetNdcDirectory",
    "expand_candidates",
    "normalize_ndc",
]

# Excel silently turns a long digit string into a float, so an NDC can arrive
# looking like "5.0242E+10" or "50242013501.0".  Only the second is recoverable.
_SCIENTIFIC = re.compile(r"^\d(?:\.\d+)?[eE][+-]?\d+$")
_TRAILING_DOT_ZERO = re.compile(r"\.0+$")
_NON_DIGIT = re.compile(r"[^0-9]")
# The only characters an NDC may contain once Excel artifacts are stripped.
# Spaces are tolerated because vendors pad cells; letters are not, because a
# value with letters is a description or a code, never an NDC.
_ALLOWED_PUNCT = re.compile(r"^[0-9\-\s]*$")

# Placeholder strings that mean "no NDC".  Kept local rather than importing
# values.NULLISH so this module stays dependency-free; the two lists overlap
# on everything that matters for an NDC column.
_NULLISH = frozenset({"nan", "none", "null", "n/a", "na", "-", ""})


class ResolutionMethod(StrEnum):
    """How an ``ndc11`` was arrived at.

    Persisted alongside the value so the reasoning behind every normalized NDC
    stays visible in the warehouse rather than being lost at load time.

    The first five are successes (``ndc11`` is set).  The last four are
    failures (``ndc11`` is ``None``) and become quarantine reason codes with
    an ``NDC_`` prefix, e.g. ``NDC_AMBIGUOUS``.
    """

    ALREADY_11 = "ALREADY_11"
    """Input was already 11 digits; accepted unchanged."""

    HYPHEN_4_4_2 = "HYPHEN_4_4_2"
    HYPHEN_5_3_2 = "HYPHEN_5_3_2"
    HYPHEN_5_4_1 = "HYPHEN_5_4_1"
    """Input was hyphenated; segment widths determined which segment to pad."""

    DIRECTORY_UNIQUE = "DIRECTORY_UNIQUE"
    """Unhyphenated 10-digit; exactly one expansion is a known NDC."""

    AMBIGUOUS = "AMBIGUOUS"
    """Unhyphenated 10-digit; two or more expansions are known NDCs."""

    NOT_FOUND = "NOT_FOUND"
    """Unhyphenated 10-digit; no expansion is known, or no directory was given."""

    INVALID = "INVALID"
    """Not an NDC at all - empty, non-numeric, or an impossible digit count."""

    LOSSY_NUMERIC = "LOSSY_NUMERIC"
    """Arrived as a float in scientific notation, so low-order digits are gone.

    Excel does this to any long digit string in a General-formatted cell.  The
    digits cannot be recovered, and the truncated value is often still 11
    digits long, so it would otherwise pass as a perfectly plausible NDC.  The
    fix belongs upstream - read the column as text - and this reason code is
    what tells an operator that.
    """


@runtime_checkable
class NdcDirectory(Protocol):
    """Anything that can answer "is this a real 11-digit NDC".

    Backed in production by the NDC dimension (FDA NDC Directory plus the CMS
    ASP crosswalk).  A protocol rather than a concrete class so normalization
    stays a pure function that is testable without a database.

    ``runtime_checkable`` so ``isinstance(x, NdcDirectory)`` works, which lets
    a caller validate what it was handed.
    """

    def contains(self, ndc11: str) -> bool: ...


class SetNdcDirectory:
    """An :class:`NdcDirectory` backed by an in-memory set.

    Used in tests and for small reference loads.
    """

    __slots__ = ("_known",)

    def __init__(self, known: set[str] | None = None) -> None:
        """Start from ``known`` (copied, so the caller's set is not shared)."""
        self._known = set(known or ())

    def contains(self, ndc11: str) -> bool:
        """Membership test; the one method the protocol requires."""
        return ndc11 in self._known

    def add(self, ndc11: str) -> None:
        """Register one 11-digit NDC as known."""
        self._known.add(ndc11)

    def __len__(self) -> int:
        return len(self._known)


@dataclass(frozen=True, slots=True)
class NdcResolution:
    """The outcome of normalizing one NDC.

    ``ndc11`` is ``None`` whenever the result is not trustworthy, so a caller
    cannot accidentally use a guess.  ``raw`` and ``method`` are persisted with
    the fact row; ``candidates`` is what an operator needs in order to resolve
    an ambiguity by hand.
    """

    raw: str
    ndc11: str | None
    method: ResolutionMethod
    candidates: tuple[str, ...] = field(default=())
    """For the 10-digit path: the expansions that were tried (NOT_FOUND) or
    the ones that matched (AMBIGUOUS).  Empty on every other path."""

    @property
    def ok(self) -> bool:
        """True when a single trustworthy 11-digit NDC was determined."""
        return self.ndc11 is not None

    @property
    def quarantine_reason(self) -> str | None:
        """Reason code for the quarantine record, or ``None`` when resolved."""
        return None if self.ok else f"NDC_{self.method.value}"


def _coerce_to_text(value: object) -> str:
    """Reduce whatever the reader handed us to a digit/hyphen string.

    Handles Excel float artifacts and the literal ``"nan"`` string that a
    ``dtype=str`` pandas read produces for an empty cell.

    Returns ``""`` for anything null-ish, which the caller treats as INVALID.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in _NULLISH:
        return ""
    # "50242013501.0" -> "50242013501": Excel's float rendering of a whole
    # number.  Scientific notation is NOT handled here; it is detected by the
    # caller and rejected, because its digits are already gone.
    return _TRAILING_DOT_ZERO.sub("", text)


def expand_candidates(digits: str) -> tuple[str, str, str]:
    """The three 11-digit readings of an unhyphenated 10-digit NDC.

    Returned in a fixed order: 4-4-2, then 5-3-2, then 5-4-1.

    Raises ``ValueError`` if ``digits`` is not exactly ten digits; callers
    are expected to have checked.

    >>> expand_candidates("2568202801")
    ('02568202801', '25682002801', '25682028001')
    """
    if len(digits) != 10 or not digits.isdigit():
        raise ValueError(f"expected 10 digits, got {digits!r}")
    # Each reading inserts one zero at a different position:
    #   4-4-2: before the labeler  -> position 0
    #   5-3-2: before the product  -> position 5
    #   5-4-1: before the package  -> position 9
    # The first of these is what zfill(11) always produced.
    return (
        "0" + digits,
        digits[:5] + "0" + digits[5:],
        digits[:9] + "0" + digits[9:],
    )


def _from_hyphenated(segments: list[str], raw: str) -> NdcResolution | None:
    """Resolve a hyphenated NDC by segment widths.

    Returns ``None`` when the widths are not a recognised configuration, so the
    caller can fall through to the digit-count rules.
    """
    # Must be exactly three all-numeric segments; anything else is not a
    # hyphenated NDC and the caller decides what to do with it.
    if len(segments) != 3 or not all(s.isdigit() for s in segments):
        return None
    labeler, product, package = segments
    widths = (len(labeler), len(product), len(package))

    # 5-4-2 is already canonical: just drop the hyphens.
    if widths == (5, 4, 2):
        return NdcResolution(raw, labeler + product + package, ResolutionMethod.ALREADY_11)
    # The three 10-digit layouts.  The hyphens tell us which segment is short,
    # so the zero goes in exactly one place and no lookup is needed.
    if widths == (4, 4, 2):
        return NdcResolution(raw, "0" + labeler + product + package, ResolutionMethod.HYPHEN_4_4_2)
    if widths == (5, 3, 2):
        return NdcResolution(raw, labeler + "0" + product + package, ResolutionMethod.HYPHEN_5_3_2)
    if widths == (5, 4, 1):
        return NdcResolution(raw, labeler + product + "0" + package, ResolutionMethod.HYPHEN_5_4_1)
    return None


def normalize_ndc(value: object, directory: NdcDirectory | None = None) -> NdcResolution:
    """Normalize any NDC representation to the canonical 11-digit 5-4-2 form.

    ``directory`` is required to resolve an unhyphenated 10-digit NDC; without
    one those return :attr:`ResolutionMethod.NOT_FOUND` rather than guessing.

    Always returns an :class:`NdcResolution`; never raises.  Check ``.ok``
    before using ``.ndc11``.

    >>> normalize_ndc("00310304000").ndc11
    '00310304000'
    >>> normalize_ndc("1234-5678-90").ndc11
    '01234567890'
    >>> normalize_ndc("12345-678-90").ndc11
    '12345067890'
    >>> normalize_ndc("12345-6789-0").ndc11
    '12345678900'
    >>> r = normalize_ndc("2568202801", SetNdcDirectory({"25682002801"}))
    >>> r.ndc11, r.method.value
    ('25682002801', 'DIRECTORY_UNIQUE')
    >>> normalize_ndc("2568202801").ok
    False
    >>> normalize_ndc("5.0242E+10").method.value
    'LOSSY_NUMERIC'
    """
    raw = _coerce_to_text(value)
    if not raw:
        # Empty or null-ish.  str(value or "") keeps the original rendering of
        # whatever was there (e.g. "nan") in the raw field for the audit trail.
        return NdcResolution(str(value or ""), None, ResolutionMethod.INVALID)

    # Checked before anything else: expanding the exponent yields a digit string
    # that is frequently 11 characters long and would sail through every rule
    # below as a valid NDC.
    if _SCIENTIFIC.match(raw):
        return NdcResolution(raw, None, ResolutionMethod.LOSSY_NUMERIC)

    # Anything but digits, hyphens and spaces is not an NDC.  Rejecting early
    # stops a product description being silently stripped down to its digits.
    if not _ALLOWED_PUNCT.match(raw):
        return NdcResolution(raw, None, ResolutionMethod.INVALID)

    # Path 1: hyphenated.  The segment widths carry the format, so this is the
    # one case a 10-digit NDC can be resolved without a directory.
    if "-" in raw:
        resolved = _from_hyphenated([s.strip() for s in raw.split("-")], raw)
        if resolved is not None:
            return resolved
        # Hyphenated but not a recognised configuration; fall through so an
        # 11-digit value with stray punctuation is still accepted.

    digits = _NON_DIGIT.sub("", raw)

    # Path 2: already eleven digits.  Accepted as-is; no way to tell a
    # mis-padded 11 from a correct one without the directory, and the
    # ground-truth test shows vendors' 11-digit columns are reliable.
    if len(digits) == 11:
        return NdcResolution(raw, digits, ResolutionMethod.ALREADY_11)
    # Neither 10 nor 11 digits is not an NDC.  Nine digits is usually a UPC
    # fragment or a truncated cell; twelve or more is a UPC or a concatenation.
    if len(digits) != 10:
        return NdcResolution(raw, None, ResolutionMethod.INVALID)

    # Path 3: ten digits, no hyphens.  This is the case the old zfill(11) got
    # wrong 54.5% of the time.  All three readings are generated and the
    # directory decides.
    candidates = expand_candidates(digits)
    if directory is None:
        # No directory means no way to choose.  NOT_FOUND rather than a guess;
        # the candidates are kept so the quarantine record shows what to check.
        return NdcResolution(raw, None, ResolutionMethod.NOT_FOUND, candidates)

    hits = tuple(c for c in candidates if directory.contains(c))
    if len(hits) == 1:
        # Exactly one reading names a real drug: that is the answer.
        return NdcResolution(raw, hits[0], ResolutionMethod.DIRECTORY_UNIQUE, candidates)
    if len(hits) > 1:
        # Two or three readings are all real drugs.  Rare, but it happens, and
        # picking one would be the same guess the old code made.  The hits
        # (not all candidates) are returned so an operator sees the choice.
        return NdcResolution(raw, None, ResolutionMethod.AMBIGUOUS, hits)
    # No reading is in the directory: either the directory is incomplete or the
    # value is not an NDC the FDA knows.  Either way, quarantine with the
    # candidates attached.
    return NdcResolution(raw, None, ResolutionMethod.NOT_FOUND, candidates)
