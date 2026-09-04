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
_ALLOWED_PUNCT = re.compile(r"^[0-9\-\s]*$")

_NULLISH = frozenset({"nan", "none", "null", "n/a", "na", "-", ""})


class ResolutionMethod(StrEnum):
    """How an ``ndc11`` was arrived at.

    Persisted alongside the value so the reasoning behind every normalized NDC
    stays visible in the warehouse rather than being lost at load time.
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
    """

    def contains(self, ndc11: str) -> bool: ...


class SetNdcDirectory:
    """An :class:`NdcDirectory` backed by an in-memory set.

    Used in tests and for small reference loads.
    """

    __slots__ = ("_known",)

    def __init__(self, known: set[str] | None = None) -> None:
        self._known = set(known or ())

    def contains(self, ndc11: str) -> bool:
        return ndc11 in self._known

    def add(self, ndc11: str) -> None:
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
    """
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in _NULLISH:
        return ""
    return _TRAILING_DOT_ZERO.sub("", text)


def expand_candidates(digits: str) -> tuple[str, str, str]:
    """The three 11-digit readings of an unhyphenated 10-digit NDC.

    Returned in a fixed order: 4-4-2, then 5-3-2, then 5-4-1.

    >>> expand_candidates("2568202801")
    ('02568202801', '25682002801', '25682028001')
    """
    if len(digits) != 10 or not digits.isdigit():
        raise ValueError(f"expected 10 digits, got {digits!r}")
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
    if len(segments) != 3 or not all(s.isdigit() for s in segments):
        return None
    labeler, product, package = segments
    widths = (len(labeler), len(product), len(package))

    if widths == (5, 4, 2):
        return NdcResolution(raw, labeler + product + package, ResolutionMethod.ALREADY_11)
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

    if "-" in raw:
        resolved = _from_hyphenated([s.strip() for s in raw.split("-")], raw)
        if resolved is not None:
            return resolved
        # Hyphenated but not a recognised configuration; fall through so an
        # 11-digit value with stray punctuation is still accepted.

    digits = _NON_DIGIT.sub("", raw)

    if len(digits) == 11:
        return NdcResolution(raw, digits, ResolutionMethod.ALREADY_11)
    if len(digits) != 10:
        return NdcResolution(raw, None, ResolutionMethod.INVALID)

    candidates = expand_candidates(digits)
    if directory is None:
        return NdcResolution(raw, None, ResolutionMethod.NOT_FOUND, candidates)

    hits = tuple(c for c in candidates if directory.contains(c))
    if len(hits) == 1:
        return NdcResolution(raw, hits[0], ResolutionMethod.DIRECTORY_UNIQUE, candidates)
    if len(hits) > 1:
        return NdcResolution(raw, None, ResolutionMethod.AMBIGUOUS, hits)
    return NdcResolution(raw, None, ResolutionMethod.NOT_FOUND, candidates)
