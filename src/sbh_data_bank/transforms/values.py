"""Parsing vendor cell values into typed, database-ready values.

Every parser returns a :class:`ParseResult` rather than raising or returning
``None``. A failure carries a reason code, which becomes the quarantine reason,
so "why is this row not in the warehouse" is always answerable without
re-running anything.

The rule throughout: **never guess, never silently null**. The previous
implementation did both, and the two most expensive defects in this codebase
came from exactly that.

Money is the sharper of the two. The Monthly Payments extract writes negative
amounts in accounting notation - ``($40.00)`` for minus forty. The previous
``clean_currency`` stripped ``$``, commas and whitespace but not parentheses,
so ``float("(40.00)")`` raised and the value became ``None``. That is 5,482 of
32,685 rows in a single month, or 16.8%, and because every one of them is a
credit or a transfer, the loss is directionally biased: totals come out
overstated rather than merely incomplete. Over forty monthly files that is the
difference between a reconciliation that ties and one that never can.
Measured across all forty files (``docs/data-findings.md``, finding 1): 227,284
rows nulled and the total overstated by $39,967,197.61.

Money never passes through ``float``. Binary floating point cannot represent
0.01, so summing a few hundred thousand payment lines through ``float``
accumulates error into the cents and sometimes the dollars. Everything
monetary is :class:`~decimal.Decimal` end to end.

The parsers, and what each returns on success:

``parse_text``       ``str``, trimmed; null-ish placeholders become empty
``parse_decimal``    ``Decimal``; handles ``$``, commas, ``(neg)``, ``neg-``;
                     at most ``QUANTITY_PLACES`` decimal places
``parse_currency``   same cleaning, at most ``CURRENCY_PLACES`` decimal places
``parse_int``        ``int``; rejects a fractional part
``parse_bool``       ``bool`` from the spellings vendors actually use
``parse_date``       ``date`` from an explicit, ordered list of formats

Every parser follows the same three-step shape: coerce the raw cell to text,
return ``empty`` if the text is a null-ish placeholder, then parse or fail
with a reason code.  The reason codes are ``TOO_LONG``, ``NOT_NUMERIC``,
``NOT_INTEGER``, ``NOT_BOOLEAN`` and ``BAD_DATE``.

How this fits
-------------
Called by ``ingest.reader`` once per cell, dispatching on the column's
declared ``FieldType`` from ``registry.spec``; ``ingest.load`` uses
``parse_currency`` directly to read a vendor's footer totals.  Depends on
nothing else in the package.  Tests: ``tests/unit/test_values.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from typing import Generic, TypeVar

__all__ = [
    "CURRENCY_PLACES",
    "MAX_TEXT_LENGTH",
    "QUANTITY_PLACES",
    "TEXT_LENGTH",
    "ParseResult",
    "parse_bool",
    "parse_currency",
    "parse_date",
    "parse_decimal",
    "parse_int",
    "parse_text",
]

T = TypeVar("T")

#: The decimal places the typed layer keeps (D15, 2026-10-21). These are the
#: scales of the ledger's ``DECIMAL`` columns - ``store.schema`` builds its
#: DDL from them - and a parsed value is rounded to them here, so the value
#: the row hash sees is the value the database stores. Nothing is lost that
#: a person can recover: the landed layer keeps the cell as delivered.
#: Evidence: WellPartner's ``Est Wholesale Cost`` carries up to seven places
#: in every delivery (a unit cost times a quantity, never rounded by the
#: TPA); every other numeric column in the nineteen sources fits.
CURRENCY_PLACES = 4
QUANTITY_PLACES = 6

#: The width, in characters, of a text column in the typed and curated
#: layers when the spec declares no ``max_length``. The parser applies the
#: same limit (2026-10-22), so a value that would not fit quarantines its
#: own row as ``TOO_LONG`` instead of failing the whole file at insert,
#: which is what a 2,620-character WeInfuse patient note did to the first
#: patients load ("String data, right truncation"). A spec that needs more
#: declares ``max_length`` up to ``MAX_TEXT_LENGTH``, the widest
#: ``NVARCHAR(n)`` SQL Server allows; the landed layer keeps the full text
#: either way.
TEXT_LENGTH = 1000
MAX_TEXT_LENGTH = 4000

#: Strings that mean "no value". Includes the literal ``"nan"`` that a pandas
#: ``dtype=str`` read produces for an empty cell - the previous wholesaler
#: parsers stored that four-character string in three of four tables, because
#: ``str(row.get(x, "") or "").strip()`` does not fire the ``or`` branch for a
#: float NaN (NaN is truthy) and ``str(nan)`` is ``"nan"``.
#: Matched case-insensitively after trimming, so "N/A", "NULL" and "  -  " all
#: count.  This is the "nullify" vocabulary the architecture doc refers to.
NULLISH = frozenset({"", "-", "--", "n/a", "na", "nan", "none", "null", "nil", "#n/a"})

# A comma that sits between a digit and exactly three digits followed by a word
# boundary: a thousands separator.  "1,234.56" matches; "1,2" does not, so a
# European decimal comma is left alone to fail loudly as NOT_NUMERIC rather
# than being silently read as 12.
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")
_CURRENCY_SYMBOLS = re.compile(r"[$£€¥]")
# The whole body wrapped in one pair of parentheses: accounting negative.
_PARENTHESISED = re.compile(r"^\((.*)\)$")
# "40.00-": a trailing sign, which some ledger exports use.  Non-greedy body
# so the sign group takes the last character.
_TRAILING_SIGN = re.compile(r"^(?P<body>.*?)(?P<sign>[+-])$")
# What a cleaned number must look like before Decimal() sees it: optional
# sign, then digits with an optional fraction, or a bare fraction like ".5".
# Decimal() itself accepts far more ("Infinity", "1e5", "NaN"), none of which
# belongs in a money or quantity column.
_DECIMAL_SHAPE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")

# Boolean spellings seen in the extracts.  Monthly Payments uses True/False;
# others use Y/N and 1/0.  Compared after lowercasing.
_TRUE = frozenset({"true", "t", "yes", "y", "1"})
_FALSE = frozenset({"false", "f", "no", "n", "0"})


@dataclass(frozen=True, slots=True)
class ParseResult(Generic[T]):
    """A parsed value, or a reason it could not be parsed.

    ``value`` is ``None`` both for a legitimately empty cell and for a failure;
    :attr:`ok` distinguishes them. That distinction matters because an empty
    optional column is normal while an unparseable one must quarantine the row.

    Three states, and how to tell them apart:

    ======  ========  ============  ====================================
    state   ``ok``    ``is_empty``  meaning
    ======  ========  ============  ====================================
    value   True      False         parsed; use ``value``
    empty   True      True          cell was blank; null in the warehouse
    failed  False     False         ``reason`` says why; quarantine the row
    ======  ========  ============  ====================================

    ``raw`` is always the cell text as received, so a quarantine record can
    show exactly what failed.
    """

    raw: str
    value: T | None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        """True when parsing succeeded, including when the cell was empty."""
        return self.reason is None

    @property
    def is_empty(self) -> bool:
        """True when the cell held no value and that was acceptable."""
        return self.reason is None and self.value is None

    @classmethod
    def empty(cls, raw: str) -> ParseResult[T]:
        """The "blank cell, and that is fine" result."""
        return cls(raw, None, None)

    @classmethod
    def failed(cls, raw: str, reason: str) -> ParseResult[T]:
        """The "could not parse" result; ``reason`` becomes the quarantine code."""
        return cls(raw, None, reason)


def _as_text(value: object) -> str:
    """Reduce any reader output to a stripped string.

    Readers hand over ``str`` from CSV, but ``int``/``float``/``datetime``
    from Excel, and a float NaN for an empty Excel cell.  Everything is
    flattened to text here so the parsers below see one shape.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, float) and value != value:  # NaN
        # NaN is the only float that is not equal to itself.  It is what an
        # empty Excel cell arrives as, and str(nan) would give "nan".
        return ""
    return str(value).strip()


def _is_nullish(text: str) -> bool:
    """Whether ``text`` is one of the placeholder spellings for "no value"."""
    return text.lower() in NULLISH


def parse_text(value: object, *, max_length: int | None = None) -> ParseResult[str]:
    """Trim a text cell, mapping null-ish placeholders to a real null.

    >>> parse_text("  CARDINAL HEALTH  ").value
    'CARDINAL HEALTH'
    >>> parse_text("nan").value is None
    True
    >>> parse_text("N/A").is_empty
    True

    Over-long values fail rather than truncating. Silent truncation is how the
    previous pipeline lost three WeInfuse patient records to a
    "String or binary data would be truncated" error that was then patched by
    cutting the value down to fit.

    >>> parse_text("abcdef", max_length=3).reason
    'TOO_LONG'
    """
    text = _as_text(value)
    if _is_nullish(text):
        return ParseResult.empty(text)
    # max_length comes from the column spec and matches the NVARCHAR width in
    # the ledger table, so a value that fails here would have failed at insert.
    if max_length is not None and len(text) > max_length:
        return ParseResult.failed(text, "TOO_LONG")
    return ParseResult(text, text)


def _clean_numeric(text: str) -> tuple[str, bool]:
    """Strip presentation from a number. Returns the body and whether it is negative.

    Handles, in order: currency symbols, thousands separators, accounting
    parentheses, and a trailing sign.  Does not validate the result; the
    caller checks the body against ``_DECIMAL_SHAPE``.
    """
    # "$3,767.71 " -> "3,767.71" -> "3767.71".  Symbols first because the
    # thousands regex needs digits on both sides of the comma.
    body = _CURRENCY_SYMBOLS.sub("", text).strip()
    body = _THOUSANDS.sub("", body).strip()

    negative = False
    # "(40.00)" is minus forty.  This is the parenthesised-negative rule that
    # closes the $39.97M defect: without it the parentheses survive, Decimal()
    # fails, and every credit in the file becomes null.
    parenthesised = _PARENTHESISED.match(body)
    if parenthesised is not None:
        negative = True
        body = parenthesised.group(1).strip()
        # A second currency symbol can sit inside the parentheses: ($40.00).
        body = _CURRENCY_SYMBOLS.sub("", body).strip()

    # "40.00-" is also minus forty.  A trailing "+" is accepted and ignored.
    # Combining with parentheses flips: "(40.00-)" would be +40, which is
    # nonsense nobody writes, but the arithmetic is at least consistent.
    trailing = _TRAILING_SIGN.match(body)
    if trailing is not None:
        if trailing.group("sign") == "-":
            negative = not negative
        body = trailing.group("body").strip()

    return body, negative


def _fit(result: ParseResult[Decimal], places: int) -> ParseResult[Decimal]:
    """Round a parsed number to ``places`` decimal places, only if it has
    more. A value that already fits is returned untouched, exponent and all,
    so ``1.50`` stays ``1.50`` and a doctest that prints it stays true."""
    value = result.value
    if value is None or not result.ok or -value.as_tuple().exponent <= places:
        return result
    return ParseResult(result.raw, value.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP))


def parse_decimal(value: object) -> ParseResult[Decimal]:
    """Parse a quantity to :class:`~decimal.Decimal`, never via ``float``,
    rounded half-up to :data:`QUANTITY_PLACES` only when it has more.

    >>> parse_decimal("1,234.56").value
    Decimal('1234.56')
    >>> parse_decimal("(40.00)").value
    Decimal('-40.00')
    >>> parse_decimal("40.00-").value
    Decimal('-40.00')
    >>> parse_decimal("0.12345675").value
    Decimal('0.123457')
    >>> parse_decimal("not a number").reason
    'NOT_NUMERIC'

    An unbalanced parenthesis is a malformed value, not a negative:

    >>> parse_decimal("(40.00").reason
    'NOT_NUMERIC'
    """
    return _fit(_parse_number(value), QUANTITY_PLACES)


def _parse_number(value: object) -> ParseResult[Decimal]:
    """The shared cleaning and parsing, at the precision the cell carries."""
    # Typed values from an Excel read are handled before any text parsing, so
    # a number stays a number and does not round-trip through str().
    if isinstance(value, Decimal):
        return ParseResult(str(value), value)
    if isinstance(value, bool):
        # bool subclasses int; a boolean in a numeric column is a schema error.
        # Must be checked before int or True would parse as 1.
        return ParseResult.failed(str(value), "NOT_NUMERIC")
    if isinstance(value, int):
        return ParseResult(str(value), Decimal(value))
    if isinstance(value, float):
        if value != value:
            # NaN: an empty Excel cell.
            return ParseResult.empty("")
        # via str() so the shortest round-trip representation is used rather
        # than the full binary expansion of the float.  Decimal(0.1) is
        # 0.1000000000000000055511151231257827...; Decimal("0.1") is 0.1.
        return ParseResult(str(value), Decimal(str(value)))

    text = _as_text(value)
    if _is_nullish(text):
        return ParseResult.empty(text)

    body, negative = _clean_numeric(text)
    # Shape check before Decimal(): Decimal accepts "1e5", "Infinity" and
    # "NaN", none of which is a vendor number, and an unbalanced "(40.00"
    # leaves a stray "(" that fails here.
    if not _DECIMAL_SHAPE.match(body):
        return ParseResult.failed(text, "NOT_NUMERIC")
    try:
        parsed = Decimal(body)
    except DecimalException:
        # Belt and braces: the shape regex should make this unreachable.
        return ParseResult.failed(text, "NOT_NUMERIC")
    # The sign is applied last, after the magnitude has parsed, so "-0.00"
    # and "(0.00)" both give Decimal("-0.00"), which compares equal to zero.
    return ParseResult(text, -parsed if negative else parsed)


def parse_currency(value: object) -> ParseResult[Decimal]:
    """Parse a monetary amount.

    Identical to :func:`parse_decimal` today; kept separate because money and
    quantity are different kinds of number and only one of them should ever
    acquire rounding or currency handling. The previous code reused its
    currency cleaner for quantities, strengths and billing units, which is how
    a unit count came to be cleaned by a function that also strips ``$``.

    The one rounding money acquires: a value with more than
    :data:`CURRENCY_PLACES` decimal places is rounded half-up to that many,
    because that is what the ledger's ``DECIMAL(38,4)`` column holds and the
    row hash must see what the database stores (D15). A cent is never touched.

    >>> parse_currency("$56,142.93").value
    Decimal('56142.93')
    >>> parse_currency("($40.00)").value
    Decimal('-40.00')
    >>> parse_currency("$3,767.71 ").value
    Decimal('3767.71')
    >>> parse_currency("12.3456789").value
    Decimal('12.3457')
    >>> parse_currency("(0.00005)").value
    Decimal('-0.0001')
    """
    return _fit(_parse_number(value), CURRENCY_PLACES)


def parse_int(value: object) -> ParseResult[int]:
    """Parse a whole number, rejecting anything with a fractional part.

    >>> parse_int("3").value
    3
    >>> parse_int("3.00").value
    3
    >>> parse_int("3.5").reason
    'NOT_INTEGER'
    """
    # Reuse the number parser so "1,000" and "(3)" work the same way here,
    # then add the one extra rule: no fractional part. Unrounded on purpose:
    # "3.0000001" must fail, not round to 3.
    result = _parse_number(value)
    if not result.ok:
        return ParseResult.failed(result.raw, result.reason or "NOT_NUMERIC")
    if result.value is None:
        return ParseResult.empty(result.raw)
    # "3.00" is a whole number and is accepted; "3.5" is not.  Comparing to
    # to_integral_value() rather than checking for "." handles both.
    if result.value != result.value.to_integral_value():
        return ParseResult.failed(result.raw, "NOT_INTEGER")
    return ParseResult(result.raw, int(result.value))


def parse_bool(value: object) -> ParseResult[bool]:
    """Parse a boolean from the spellings vendors actually use.

    >>> parse_bool("True").value, parse_bool("N").value, parse_bool("1").value
    (True, False, True)
    >>> parse_bool("maybe").reason
    'NOT_BOOLEAN'
    """
    # An Excel read can hand over a real bool; pass it through.
    if isinstance(value, bool):
        return ParseResult(str(value), value)
    text = _as_text(value)
    if _is_nullish(text):
        return ParseResult.empty(text)
    lowered = text.lower()
    if lowered in _TRUE:
        return ParseResult(text, True)
    if lowered in _FALSE:
        return ParseResult(text, False)
    # Anything outside the two spelling sets is a failure, not False.  A
    # column that suddenly contains "Yes " with a trailing tab or "TRUE1"
    # should quarantine, not silently read as False.
    return ParseResult.failed(text, "NOT_BOOLEAN")


def parse_date(value: object, formats: list[str]) -> ParseResult[date]:
    """Parse a date against an explicit list of formats, in order.

    ``formats`` is required and has no default. This is the point of the
    function. The previous implementation declared a format parameter and never
    passed it, leaving every date to ``pandas`` inference - and the Cardinal
    detail report ships ``2024-09-16`` in one pull and ``01/01/2025`` in the
    next, while a value like ``01/02/2025`` is genuinely ambiguous between
    2 January and 1 February. Inference resolves that by looking at the rest of
    the column, so the same string can parse differently depending on what else
    is in the file. Declaring the format makes a vendor changing it a loud
    failure instead of a silent reinterpretation.

    Raises ``ValueError`` if ``formats`` is empty - that is a spec bug, not a
    data problem, so it is not a ``ParseResult``.

    >>> parse_date("2024-09-16", ["%Y-%m-%d"]).value
    datetime.date(2024, 9, 16)
    >>> parse_date("01/02/2025", ["%m/%d/%Y"]).value
    datetime.date(2025, 1, 2)
    >>> parse_date("01/02/2025", ["%d/%m/%Y"]).value
    datetime.date(2025, 2, 1)
    >>> parse_date("6/17/2025", ["%m/%d/%Y"]).value
    datetime.date(2025, 6, 17)
    >>> parse_date("Jun 2025", ["%b %Y"]).value
    datetime.date(2025, 6, 1)
    >>> parse_date("2025-13-01", ["%Y-%m-%d"]).reason
    'BAD_DATE'
    """
    if not formats:
        raise ValueError("parse_date requires at least one explicit format")

    # Excel hands over real datetime objects for date-formatted cells.  Take
    # the date part; the time part of a date column is always midnight.
    # datetime before date because datetime is a subclass of date.
    if isinstance(value, datetime):
        return ParseResult(value.isoformat(), value.date())
    if isinstance(value, date):
        return ParseResult(value.isoformat(), value)

    text = _as_text(value)
    if _is_nullish(text):
        return ParseResult.empty(text)

    # Try each declared format in order; the first that parses wins.  Order
    # matters only when two formats could both match the same string, which
    # the specs avoid: Cardinal declares ISO then US, and "2024-09-16" can
    # never satisfy %m/%d/%Y, so there is no overlap.  strptime is strict -
    # "2025-13-01" raises rather than rolling the month over.
    for fmt in formats:
        try:
            return ParseResult(text, datetime.strptime(text, fmt).date())
        except ValueError:
            continue
    # None of the declared formats matched.  This is the "a declared format
    # that no longer matches must stop the load" behaviour: WeInfuse Patients
    # quarantined all 1,918 rows on first run because the wrong shape was
    # declared, which is the intended outcome (data-findings, finding 11).
    return ParseResult.failed(text, "BAD_DATE")
