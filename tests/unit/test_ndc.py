"""Tests for NDC normalization.

The cases that matter are the ones the previous implementation got wrong, so
those are asserted explicitly against the old behaviour rather than only
against the new expected value.
"""

from __future__ import annotations

import pytest

from sbh_data_bank.transforms.ndc import (
    NdcResolution,
    ResolutionMethod,
    SetNdcDirectory,
    expand_candidates,
    normalize_ndc,
)


def _old_zfill(value: str) -> str:
    """The previous implementation, reproduced so the regression is explicit.

    Equivalent to both ``src/parsers/base.py::normalise_ndc`` and the SQL
    ``RIGHT('00000000000' + REPLACE(ndc,'-',''), 11)``.
    """
    import re

    return re.sub(r"[^0-9]", "", str(value)).zfill(11)


class TestElevenDigitPassThrough:
    def test_plain_11_digit_is_accepted_unchanged(self):
        r = normalize_ndc("00310304000")
        assert r.ndc11 == "00310304000"
        assert r.method is ResolutionMethod.ALREADY_11
        assert r.ok

    def test_hyphenated_5_4_2_is_accepted(self):
        r = normalize_ndc("00310-3040-00")
        assert r.ndc11 == "00310304000"
        assert r.method is ResolutionMethod.ALREADY_11

    def test_whitespace_is_tolerated(self):
        assert normalize_ndc("  61958250101  ").ndc11 == "61958250101"


class TestHyphenatedTenDigit:
    """Segment widths make the configuration unambiguous."""

    @pytest.mark.parametrize(
        ("raw", "expected", "method"),
        [
            ("1234-5678-90", "01234567890", ResolutionMethod.HYPHEN_4_4_2),
            ("12345-678-90", "12345067890", ResolutionMethod.HYPHEN_5_3_2),
            ("12345-6789-0", "12345678900", ResolutionMethod.HYPHEN_5_4_1),
        ],
    )
    def test_pads_the_deficient_segment(self, raw, expected, method):
        r = normalize_ndc(raw)
        assert r.ndc11 == expected
        assert r.method is method

    def test_old_implementation_was_wrong_for_5_3_2_and_5_4_1(self):
        # 4-4-2 is the one case a left-pad happens to get right.
        assert _old_zfill("1234-5678-90") == normalize_ndc("1234-5678-90").ndc11
        # The other two it corrupts.
        assert _old_zfill("12345-678-90") != normalize_ndc("12345-678-90").ndc11
        assert _old_zfill("12345-6789-0") != normalize_ndc("12345-6789-0").ndc11

    def test_no_directory_needed_when_hyphenated(self):
        assert normalize_ndc("12345-678-90", directory=None).ok


class TestUnhyphenatedTenDigit:
    """The configuration is unrecoverable from digits alone; resolve or refuse."""

    def test_all_three_expansions_are_generated(self):
        assert expand_candidates("2568202801") == (
            "02568202801",
            "25682002801",
            "25682028001",
        )

    def test_refuses_to_guess_without_a_directory(self):
        r = normalize_ndc("2568202801")
        assert not r.ok
        assert r.ndc11 is None
        assert r.method is ResolutionMethod.NOT_FOUND
        assert len(r.candidates) == 3
        assert r.quarantine_reason == "NDC_NOT_FOUND"

    def test_mckesson_real_pair_resolves_to_vendor_truth(self):
        """Straight out of ``McKesson_Custom Report``.

        The export carries ``10 Digit NDC (Current) = 2568202801`` and
        ``NDC (Current) = 25682002801`` on the same row, so the vendor tells us
        the right answer.  It is a 5-3-2 NDC.
        """
        directory = SetNdcDirectory({"25682002801"})
        r = normalize_ndc("2568202801", directory)

        assert r.ndc11 == "25682002801"
        assert r.method is ResolutionMethod.DIRECTORY_UNIQUE
        # And the old code produced a different labeler entirely.
        assert _old_zfill("2568202801") == "02568202801"
        assert _old_zfill("2568202801") != r.ndc11

    def test_ambiguous_when_two_expansions_are_real(self):
        directory = SetNdcDirectory({"02568202801", "25682002801"})
        r = normalize_ndc("2568202801", directory)

        assert not r.ok
        assert r.method is ResolutionMethod.AMBIGUOUS
        assert set(r.candidates) == {"02568202801", "25682002801"}
        assert r.quarantine_reason == "NDC_AMBIGUOUS"

    def test_not_found_when_no_expansion_is_real(self):
        r = normalize_ndc("2568202801", SetNdcDirectory({"99999999999"}))
        assert not r.ok
        assert r.method is ResolutionMethod.NOT_FOUND


class TestJunkInput:
    @pytest.mark.parametrize(
        "value",
        [None, "", "   ", "nan", "NaN", "N/A", "null", "-", "NONE"],
    )
    def test_nullish_values_are_invalid_not_zero_padded(self, value):
        r = normalize_ndc(value)
        assert not r.ok
        assert r.method is ResolutionMethod.INVALID

    def test_old_implementation_turned_nan_into_a_plausible_ndc(self):
        """``zfill`` on ``"nan"`` yielded ``"00000000000"`` - a value that looks
        like a real NDC and joins against other rows that did the same."""
        assert _old_zfill("nan") == "00000000000"
        assert normalize_ndc("nan").ndc11 is None

    def test_a_drug_description_is_rejected_not_stripped_to_digits(self):
        r = normalize_ndc("ULTOMIRIS 1100 MG/11 ML VIAL")
        assert not r.ok
        assert r.method is ResolutionMethod.INVALID

    @pytest.mark.parametrize("value", ["123", "123456789", "123456789012"])
    def test_impossible_digit_counts_are_invalid(self, value):
        assert normalize_ndc(value).method is ResolutionMethod.INVALID


class TestExcelArtifacts:
    def test_trailing_dot_zero_from_a_float_read(self):
        assert normalize_ndc("50242013501.0").ndc11 == "50242013501"

    def test_scientific_notation_is_not_silently_accepted(self):
        """Excel has already destroyed the low-order digits; the value must not
        be treated as a real NDC."""
        r = normalize_ndc("5.0242E+10")
        assert not r.ok


class TestResolutionContract:
    def test_result_is_immutable(self):
        r = normalize_ndc("00310304000")
        with pytest.raises(AttributeError):
            r.ndc11 = "99999999999"  # type: ignore[misc]

    def test_unresolved_never_exposes_a_guess(self):
        r = normalize_ndc("2568202801")
        assert r.ndc11 is None
        assert r.candidates  # the guesses are available, but not as the answer

    def test_raw_is_preserved_for_audit(self):
        assert normalize_ndc("12345-678-90").raw == "12345-678-90"

    def test_ok_and_quarantine_reason_are_mutually_exclusive(self):
        for value in ["00310304000", "2568202801", "nan", "12345-678-90"]:
            r: NdcResolution = normalize_ndc(value)
            assert r.ok == (r.quarantine_reason is None)
