"""Tests for value parsing.

Values are taken from the real extracts wherever possible, so the suite fails
if a parser stops handling a file that is actually delivered.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from sbh_data_bank.transforms.values import (
    parse_bool,
    parse_currency,
    parse_date,
    parse_decimal,
    parse_int,
    parse_text,
)


def _old_clean_currency(val):
    """The previous implementation, for regression comparison.

    ``src/parsers/base.py::clean_currency``.
    """
    import re

    s = re.sub(r"[$,\s]", "", str(val))
    if s in ("", "-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


class TestAccountingNegatives:
    """The defect that would have silently dropped 16.8% of every payments file."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("($40.00)", Decimal("-40.00")),
            ("($30.00)", Decimal("-30.00")),
            ("($24.88)", Decimal("-24.88")),
            ("($1,234.56)", Decimal("-1234.56")),
            ("(40.00)", Decimal("-40.00")),
        ],
    )
    def test_parenthesised_amounts_are_negative(self, raw, expected):
        assert parse_currency(raw).value == expected

    def test_the_old_implementation_nulled_every_one_of_these(self):
        """Not merely wrong - wrong in one direction. Every parenthesised value
        is a credit or a transfer, so nulling them overstates every total."""
        for raw in ("($40.00)", "($30.00)", "($1,234.56)"):
            assert _old_clean_currency(raw) is None
            assert parse_currency(raw).value < 0

    def test_a_realistic_month_totals_correctly(self):
        """Mirrors the shape of one Monthly Payments file: mostly positive
        charges, a minority of parenthesised credits."""
        amounts = ["$100.00"] * 8 + ["($40.00)"] * 2
        total = sum(parse_currency(a).value for a in amounts)
        assert total == Decimal("720.00")

        old_total = sum(v for v in (_old_clean_currency(a) for a in amounts) if v is not None)
        assert old_total == 800.0  # the credits vanished, overstating by 80

    def test_unbalanced_parenthesis_is_malformed_not_negative(self):
        assert parse_currency("(40.00").reason == "NOT_NUMERIC"
        assert parse_currency("40.00)").reason == "NOT_NUMERIC"

    def test_trailing_minus_is_negative(self):
        assert parse_decimal("40.00-").value == Decimal("-40.00")

    def test_parenthesised_negative_is_not_double_negated(self):
        assert parse_decimal("(-40.00)").value == Decimal("40.00")


class TestCurrencyFormatting:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("$56,142.93", Decimal("56142.93")),  # McKesson Extended Price
            ("$3,767.71 ", Decimal("3767.71")),  # Cardinal, trailing space
            ("2,716.96", Decimal("2716.96")),  # Cardinal, quoted thousands
            ("$0.00", Decimal("0.00")),
            ("29,459.68", Decimal("29459.68")),  # McKesson AWP
            ("$2251.22", Decimal("2251.22")),  # Pharmaforce, no separator
        ],
    )
    def test_real_vendor_amounts(self, raw, expected):
        assert parse_currency(raw).value == expected

    def test_money_never_passes_through_float(self):
        """Summing through float accumulates error into the cents. These three
        values sum exactly in Decimal and do not in binary floating point."""
        parts = ["0.10", "0.20", "0.30"]
        assert sum(parse_currency(p).value for p in parts) == Decimal("0.60")
        assert sum(float(p) for p in parts) != 0.6

    def test_precision_is_preserved_on_large_amounts(self):
        assert parse_currency("$1,234,567.89").value == Decimal("1234567.89")

    def test_thousands_separator_only_strips_true_groupings(self):
        """A comma not in a thousands position is a malformed number, not a
        separator to be quietly removed."""
        assert parse_decimal("1,23").reason == "NOT_NUMERIC"
        assert parse_decimal("1,234").value == Decimal("1234")


class TestNullish:
    @pytest.mark.parametrize("raw", ["", "  ", "-", "N/A", "n/a", "nan", "NaN", "null", "NULL"])
    def test_nullish_values_become_null_not_zero(self, raw):
        result = parse_currency(raw)
        assert result.is_empty
        assert result.value is None

    def test_the_literal_nan_string_never_reaches_a_text_column(self):
        """Three of four wholesaler parsers stored the four-character string
        "nan" because a float NaN is truthy, so `... or ""` never fired."""
        assert parse_text("nan").value is None
        assert parse_text(float("nan")).value is None

    def test_empty_is_distinct_from_a_parse_failure(self):
        empty, failed = parse_currency(""), parse_currency("abc")
        assert empty.ok and empty.is_empty
        assert not failed.ok and failed.reason == "NOT_NUMERIC"


class TestText:
    def test_trims_whitespace(self):
        assert parse_text("  CARDINAL HEALTH  ").value == "CARDINAL HEALTH"

    def test_trailing_whitespace_in_codes_is_removed(self):
        """Monthly Payments ships 'UNASGN  ' with trailing spaces."""
        assert parse_text("UNASGN  ").value == "UNASGN"

    def test_over_length_fails_rather_than_truncating(self):
        """Silent truncation is how three WeInfuse patient rows were lost."""
        result = parse_text("abcdef", max_length=3)
        assert result.reason == "TOO_LONG"
        assert result.value is None

    def test_embedded_newlines_are_preserved(self):
        """Master COGS List Location holds multi-line ship-to addresses."""
        assert (
            parse_text("ROSEMONT ORL\n8379 W SUNSET RD").value == "ROSEMONT ORL\n8379 W SUNSET RD"
        )


class TestIntegers:
    def test_whole_numbers(self):
        assert parse_int("3").value == 3
        assert parse_int("3.00").value == 3
        assert parse_int(3).value == 3

    def test_fractional_values_are_rejected_not_truncated(self):
        """The previous helpers did int(float(x)), turning 3.5 into 3 silently.
        A fractional value in a unit-count column is a real signal."""
        assert parse_int("3.5").reason == "NOT_INTEGER"

    def test_negative_and_parenthesised(self):
        assert parse_int("(3)").value == -3


class TestBooleans:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("True", True), ("False", False), ("Y", True), ("N", False), ("1", True), ("0", False)],
    )
    def test_vendor_boolean_spellings(self, raw, expected):
        assert parse_bool(raw).value is expected

    def test_is_voided_from_monthly_payments(self):
        assert parse_bool("False").value is False
        assert parse_bool("True").value is True

    def test_unknown_spelling_fails(self):
        assert parse_bool("maybe").reason == "NOT_BOOLEAN"


class TestDates:
    def test_formats_are_required(self):
        with pytest.raises(ValueError, match="explicit format"):
            parse_date("2024-09-16", [])

    def test_the_two_cardinal_formats(self):
        """The same Cardinal detail report ships ISO in one pull and US in the
        next, so the spec declares one format per variant."""
        assert parse_date("2024-09-16", ["%Y-%m-%d"]).value == date(2024, 9, 16)
        assert parse_date("01/01/2025", ["%m/%d/%Y"]).value == date(2025, 1, 1)

    def test_an_ambiguous_date_resolves_by_the_declared_format_only(self):
        """01/02/2025 is 2 January or 1 February depending on the format. With
        inference the answer depends on the rest of the column; with a declared
        format it is deterministic."""
        assert parse_date("01/02/2025", ["%m/%d/%Y"]).value == date(2025, 1, 2)
        assert parse_date("01/02/2025", ["%d/%m/%Y"]).value == date(2025, 2, 1)

    def test_a_value_in_the_wrong_format_fails_loudly(self):
        """A vendor switching format must stop the load, not be reinterpreted."""
        assert parse_date("01/01/2025", ["%Y-%m-%d"]).reason == "BAD_DATE"

    def test_monthly_payments_formats(self):
        assert parse_date("6/17/2025", ["%m/%d/%Y"]).value == date(2025, 6, 17)
        assert parse_date("Jun 2025", ["%b %Y"]).value == date(2025, 6, 1)

    def test_format_list_is_tried_in_order(self):
        formats = ["%Y-%m-%d", "%m/%d/%Y"]
        assert parse_date("2024-09-16", formats).value == date(2024, 9, 16)
        assert parse_date("01/01/2025", formats).value == date(2025, 1, 1)

    def test_impossible_dates_are_rejected(self):
        assert parse_date("2025-13-01", ["%Y-%m-%d"]).reason == "BAD_DATE"
        assert parse_date("2025-02-30", ["%Y-%m-%d"]).reason == "BAD_DATE"

    def test_datetime_from_an_excel_read_passes_through(self):
        assert parse_date(datetime(2025, 6, 17, 9, 30), ["%Y-%m-%d"]).value == date(2025, 6, 17)

    def test_empty_date(self):
        assert parse_date("", ["%Y-%m-%d"]).is_empty


class TestReaderTypeVariation:
    """A CSV read yields strings where an Excel read yields typed values. Both
    must land the same, or a vendor switching format supersedes all history."""

    def test_excel_int_and_csv_string_agree(self):
        assert parse_decimal(3).value == parse_decimal("3").value

    def test_excel_float_and_csv_string_agree(self):
        assert parse_decimal(3767.71).value == parse_decimal("3767.71").value

    def test_float_conversion_does_not_leak_binary_expansion(self):
        """Decimal(0.1) is 0.1000000000000000055511151231257827; via str it is 0.1."""
        assert parse_decimal(0.1).value == Decimal("0.1")

    def test_nan_float_is_empty_not_a_failure(self):
        assert parse_decimal(float("nan")).is_empty

    def test_a_boolean_in_a_numeric_column_is_an_error(self):
        assert parse_decimal(True).reason == "NOT_NUMERIC"
