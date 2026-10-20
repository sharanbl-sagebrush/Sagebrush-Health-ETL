"""Tests for NDC normalization - ``sbh_data_bank.transforms.ndc``.

An NDC is written by the FDA as 10 digits in one of three segment layouts
(4-4-2, 5-3-2, 5-4-1) and stored by everyone else as 11 digits with a leading
zero inserted into the short segment. Which segment is short is not
recoverable from an unhyphenated 10-digit string, so the previous left-pad
(``zfill(11)``) was wrong on 54.5% of distinct NDCs - see
docs/data-findings.md finding 2.

The cases that matter are the ones the previous implementation got wrong, so
those are asserted explicitly against the old behaviour rather than only
against the new expected value.

How to read this file:

- TestElevenDigitPassThrough  already 11 digits: accept
- TestHyphenatedTenDigit      hyphens give the segment widths: pad the short one
- TestUnhyphenatedTenDigit    no hyphens: look up all three expansions, or refuse
- TestJunkInput               nullish and non-NDC text is invalid, never padded
- TestExcelArtifacts          float and scientific-notation damage from spreadsheets
- TestResolutionContract      the shape of the result object
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
    """Eleven digits is already the canonical form; nothing to resolve."""

    def test_plain_11_digit_is_accepted_unchanged(self):
        """The common case: the vendor already sends 11 digits."""
        r = normalize_ndc("00310304000")
        assert r.ndc11 == "00310304000"
        assert r.method is ResolutionMethod.ALREADY_11
        assert r.ok

    def test_hyphenated_5_4_2_is_accepted(self):
        """5-4-2 is the hyphenated 11-digit form; strip the hyphens and accept."""
        r = normalize_ndc("00310-3040-00")
        assert r.ndc11 == "00310304000"
        assert r.method is ResolutionMethod.ALREADY_11

    def test_whitespace_is_tolerated(self):
        """Padding from a CSV cell is not part of the code."""
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
        """The zero goes into whichever segment is one digit short, and the
        method records which layout was seen."""
        r = normalize_ndc(raw)
        assert r.ndc11 == expected
        assert r.method is method

    def test_old_implementation_was_wrong_for_5_3_2_and_5_4_1(self):
        """The regression, stated directly: a left-pad only ever puts the zero
        at the front, which is the 4-4-2 answer."""
        # 4-4-2 is the one case a left-pad happens to get right.
        assert _old_zfill("1234-5678-90") == normalize_ndc("1234-5678-90").ndc11
        # The other two it corrupts.
        assert _old_zfill("12345-678-90") != normalize_ndc("12345-678-90").ndc11
        assert _old_zfill("12345-6789-0") != normalize_ndc("12345-6789-0").ndc11

    def test_no_directory_needed_when_hyphenated(self):
        """Hyphens carry the answer, so this path works with no NDC dimension."""
        assert normalize_ndc("12345-678-90", directory=None).ok


class TestUnhyphenatedTenDigit:
    """The configuration is unrecoverable from digits alone; resolve or refuse."""

    def test_all_three_expansions_are_generated(self):
        """One 10-digit string has exactly three possible 11-digit readings."""
        assert expand_candidates("2568202801") == (
            "02568202801",
            "25682002801",
            "25682028001",
        )

    def test_refuses_to_guess_without_a_directory(self):
        """No directory means no way to choose, so the result is a quarantine
        with the three candidates attached for a human to look at."""
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
        """If two expansions both exist in the directory, picking either would
        be a guess. Quarantine as AMBIGUOUS with both candidates."""
        directory = SetNdcDirectory({"02568202801", "25682002801"})
        r = normalize_ndc("2568202801", directory)

        assert not r.ok
        assert r.method is ResolutionMethod.AMBIGUOUS
        assert set(r.candidates) == {"02568202801", "25682002801"}
        assert r.quarantine_reason == "NDC_AMBIGUOUS"

    def test_not_found_when_no_expansion_is_real(self):
        """A directory that knows none of the three is a NOT_FOUND, which also
        covers a directory that is simply out of date."""
        r = normalize_ndc("2568202801", SetNdcDirectory({"99999999999"}))
        assert not r.ok
        assert r.method is ResolutionMethod.NOT_FOUND


class TestJunkInput:
    """Values that are not NDCs at all. The old code zero-padded these into
    plausible-looking codes; the new code refuses them."""

    @pytest.mark.parametrize(
        "value",
        [None, "", "   ", "nan", "NaN", "N/A", "null", "-", "NONE"],
    )
    def test_nullish_values_are_invalid_not_zero_padded(self, value):
        """Every spelling of "nothing" a reader can produce is INVALID."""
        r = normalize_ndc(value)
        assert not r.ok
        assert r.method is ResolutionMethod.INVALID

    def test_old_implementation_turned_nan_into_a_plausible_ndc(self):
        """``zfill`` on ``"nan"`` yielded ``"00000000000"`` - a value that looks
        like a real NDC and joins against other rows that did the same."""
        assert _old_zfill("nan") == "00000000000"
        assert normalize_ndc("nan").ndc11 is None

    def test_a_drug_description_is_rejected_not_stripped_to_digits(self):
        """The old regex kept only the digits, so a description containing a
        dose would become an NDC. Text is rejected whole."""
        r = normalize_ndc("ULTOMIRIS 1100 MG/11 ML VIAL")
        assert not r.ok
        assert r.method is ResolutionMethod.INVALID

    @pytest.mark.parametrize("value", ["123", "123456789", "123456789012"])
    def test_impossible_digit_counts_are_invalid(self, value):
        """Only 10 or 11 digits can be an NDC; anything else is INVALID, not
        padded or truncated."""
        assert normalize_ndc(value).method is ResolutionMethod.INVALID


class TestExcelArtifacts:
    """Damage a spreadsheet does to a numeric-looking code on the way out."""

    def test_trailing_dot_zero_from_a_float_read(self):
        """An NDC read as a float comes back as "50242013501.0"; the ".0" is
        an artifact, not part of the code."""
        assert normalize_ndc("50242013501.0").ndc11 == "50242013501"

    def test_scientific_notation_is_not_silently_accepted(self):
        """Excel has already destroyed the low-order digits; the value must not
        be treated as a real NDC."""
        r = normalize_ndc("5.0242E+10")
        assert not r.ok


class TestResolutionContract:
    """The shape of ``NdcResolution``: what every caller can rely on."""

    def test_result_is_immutable(self):
        """A resolution is a fact; nothing downstream may edit it."""
        r = normalize_ndc("00310304000")
        with pytest.raises(AttributeError):
            r.ndc11 = "99999999999"  # type: ignore[misc]

    def test_unresolved_never_exposes_a_guess(self):
        """``ndc11`` is None unless the answer is certain; the candidates are
        on a separate field so they cannot be mistaken for the answer."""
        r = normalize_ndc("2568202801")
        assert r.ndc11 is None
        assert r.candidates  # the guesses are available, but not as the answer

    def test_raw_is_preserved_for_audit(self):
        """The vendor's original string is kept, so the resolution can always
        be re-examined."""
        assert normalize_ndc("12345-678-90").raw == "12345-678-90"

    def test_ok_and_quarantine_reason_are_mutually_exclusive(self):
        """Exactly one of "accepted" and "here is why not" is ever set."""
        for value in ["00310304000", "2568202801", "nan", "12345-678-90"]:
            r: NdcResolution = normalize_ndc(value)
            assert r.ok == (r.quarantine_reason is None)
