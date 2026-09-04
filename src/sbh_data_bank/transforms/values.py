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

Money never passes through ``float``. Binary floating point cannot represent
0.01, so summing a few hundred thousand payment lines through ``float``
accumulates error into the cents and sometimes the dollars. Everything
monetary is :class:`~decimal.Decimal` end to end.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, DecimalException
from typing import Generic, TypeVar

__all__ = [
    "ParseResult",
    "parse_bool",
    "parse_currency",
    "parse_date",
    "parse_decimal",
    "parse_int",
    "parse_text",
]

T = TypeVar("T")

#: Strings that mean "no value". Includes the literal ``"nan"`` that a pandas
#: ``dtype=str`` read produces for an empty cell - the previous wholesaler
#: parsers stored that four-character string in three of four tables, because
#: ``str(row.get(x, "") or "").strip()`` does not fire the ``or`` branch for a
#: float NaN (NaN is truthy) and ``str(nan)`` is ``"nan"``.
NULLISH = frozenset({"", "-", "--", "n/a", "na", "nan", "none", "null", "nil", "#n/a"})

_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")
_CURRENCY_SYMBOLS = re.compile(r"[$£€¥]")
_PARENTHESISED = re.compile(r"^\((.*)\)$")
_TRAILING_SIGN = re.compile(r"^(?P<body>.*?)(?P<sign>[+-])$")
_DECIMAL_SHAPE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")

_TRUE = frozenset({"true", "t", "yes", "y", "1"})
_FALSE = frozenset({"false", "f", "no", "n", "0"})


@dataclass(frozen=True, slots=True)
class ParseResult(Generic[T]):
    """A parsed value, or a reason it could not be parsed.

    ``value`` is ``None`` both for a legitimately empty cell and for a failure;
    :attr:`ok` distinguishes them. That distinction matters because an empty
    optional column is normal while an unparseable one must quarantine the row.
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
        return cls(raw, None, None)

    @classmethod
    def failed(cls, raw: str, reason: str) -> ParseResult[T]:
        return cls(raw, None, reason)


def _as_text(value: object) -> str:
    """Reduce any reader output to a stripped string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, float) and value != value:  # NaN
        return ""
    return str(value).strip()


def _is_nullish(text: str) -> bool:
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
    if max_length is not None and len(text) > max_length:
        return ParseResult.failed(text, "TOO_LONG")
    return ParseResult(text, text)


def _clean_numeric(text: str) -> tuple[str, bool]:
    """Strip presentation from a number. Returns the body and whether it is negative.

    Handles, in order: currency symbols, thousands separators, accounting
    parentheses, and a trailing sign.
    """
    body = _CURRENCY_SYMBOLS.sub("", text).strip()
    body = _THOUSANDS.sub("", body).strip()

    negative = False
    parenthesised = _PARENTHESISED.match(body)
    if parenthesised is not None:
        negative = True
        body = parenthesised.group(1).strip()
        # A second currency symbol can sit inside the parentheses: ($40.00).
        body = _CURRENCY_SYMBOLS.sub("", body).strip()

    trailing = _TRAILING_SIGN.match(body)
    if trailing is not None:
        if trailing.group("sign") == "-":
            negative = not negative
        body = trailing.group("body").strip()

    return body, negative


def parse_decimal(value: object) -> ParseResult[Decimal]:
    """Parse a number to :class:`~decimal.Decimal`, never via ``float``.

    >>> parse_decimal("1,234.56").value
    Decimal('1234.56')
    >>> parse_decimal("(40.00)").value
    Decimal('-40.00')
    >>> parse_decimal("40.00-").value
    Decimal('-40.00')
    >>> parse_decimal("not a number").reason
    'NOT_NUMERIC'

    An unbalanced parenthesis is a malformed value, not a negative:

    >>> parse_decimal("(40.00").reason
    'NOT_NUMERIC'
    """
    if isinstance(value, Decimal):
        return ParseResult(str(value), value)
    if isinstance(value, bool):
        # bool subclasses int; a boolean in a numeric column is a schema error.
        return ParseResult.failed(str(value), "NOT_NUMERIC")
    if isinstance(value, int):
        return ParseResult(str(value), Decimal(value))
    if isinstance(value, float):
        if value != value:
            return ParseResult.empty("")
        # via str() so the shortest round-trip representation is used rather
        # than the full binary expansion of the float.
        return ParseResult(str(value), Decimal(str(value)))

    text = _as_text(value)
    if _is_nullish(text):
        return ParseResult.empty(text)

    body, negative = _clean_numeric(text)
    if not _DECIMAL_SHAPE.match(body):
        return ParseResult.failed(text, "NOT_NUMERIC")
    try:
        parsed = Decimal(body)
    except DecimalException:
        return ParseResult.failed(text, "NOT_NUMERIC")
    return ParseResult(text, -parsed if negative else parsed)


def parse_currency(value: object) -> ParseResult[Decimal]:
    """Parse a monetary amount.

    Identical to :func:`parse_decimal` today; kept separate because money and
    quantity are different kinds of number and only one of them should ever
    acquire rounding or currency handling. The previous code reused its
    currency cleaner for quantities, strengths and billing units, which is how
    a unit count came to be cleaned by a function that also strips ``$``.

    >>> parse_currency("$56,142.93").value
    Decimal('56142.93')
    >>> parse_currency("($40.00)").value
    Decimal('-40.00')
    >>> parse_currency("$3,767.71 ").value
    Decimal('3767.71')
    """
    return parse_decimal(value)


def parse_int(value: object) -> ParseResult[int]:
    """Parse a whole number, rejecting anything with a fractional part.

    >>> parse_int("3").value
    3
    >>> parse_int("3.00").value
    3
    >>> parse_int("3.5").reason
    'NOT_INTEGER'
    """
    result = parse_decimal(value)
    if not result.ok:
        return ParseResult.failed(result.raw, result.reason or "NOT_NUMERIC")
    if result.value is None:
        return ParseResult.empty(result.raw)
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

    if isinstance(value, datetime):
        return ParseResult(value.isoformat(), value.date())
    if isinstance(value, date):
        return ParseResult(value.isoformat(), value)

    text = _as_text(value)
    if _is_nullish(text):
        return ParseResult.empty(text)

    for fmt in formats:
        try:
            return ParseResult(text, datetime.strptime(text, fmt).date())
        except ValueError:
            continue
    return ParseResult.failed(text, "BAD_DATE")
