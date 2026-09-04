"""Tests for vendor header matching.

Cases are taken from headers that actually appear in the Sagebrush extracts,
so the suite fails if normalization stops handling a real file.
"""

from __future__ import annotations

import pytest

from sbh_data_bank.registry.headers import (
    AmbiguousHeaderError,
    HeaderIndex,
    is_junk_header,
    normalize_header,
)

# Verbatim from Cardinal_Invoice Review, 17-column detail variant.
CARDINAL_DETAIL = [
    "Account #",
    "Account Name",
    "Account Alias",
    "PO #",
    "Invoice #",
    "Invoice Line #",
    "Invoice Date",
    "Invoice Type",
    "Purchase/Return",
    "CIN",
    "NDC",
    "Trade Name",
    "Generic Name",
    "Unit price (Current Invoice)",
    "Original Ordered Qty",
    "Ship Qty",
    "Purchase Dollars (Exclude Taxes, Fees & Surcharges)",
]

# Verbatim from the 9-column Cardinal invoice-summary variant.
CARDINAL_SUMMARY = [
    "Account #",
    "Account Name",
    "Account Alias",
    "PO #",
    "Invoice #",
    "Invoice Date",
    "Invoice Type",
    "Purchase/Return",
    "Purchase Dollars (Exclude Taxes, Fees & Surcharges)",
]


class TestNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Account #", "account"),
            ("PO #", "po"),
            ("Invoice Line #", "invoice_line"),
            ("Purchase/Return", "purchase_return"),
            ("Unit price (Current Invoice)", "unit_price_current_invoice"),
            (
                "Purchase Dollars (Exclude Taxes, Fees & Surcharges)",
                "purchase_dollars_exclude_taxes_fees_surcharges",
            ),
            ("Billing Code (HCPCS)", "billing_code_hcpcs"),
            ("Est 340B Cost", "est_340b_cost"),
            ("  padded  ", "padded"),
            ("MiXeD CaSe", "mixed_case"),
        ],
    )
    def test_real_vendor_headers(self, raw, expected):
        assert normalize_header(raw) == expected

    def test_the_wellpartner_backtick_typo_normalizes_away(self):
        """The vendor ships ``Days` Supply``. Matching it byte-for-byte means
        the field silently goes null the day they fix their own header."""
        assert normalize_header("Days` Supply") == normalize_header("Days Supply")

    def test_allscripts_renamed_column_is_not_silently_unified(self):
        """LOCATION_NAME1 and LOCATION_NAME are different columns; the spec
        handles them with an alias list, not by normalization."""
        assert normalize_header("LOCATION_NAME1") != normalize_header("LOCATION_NAME")

    def test_mckesson_two_ndc_columns_stay_distinct(self):
        assert normalize_header("10 Digit NDC (Current)") != normalize_header("NDC (Current)")

    def test_similar_but_different_columns_are_not_unified(self):
        """No stemming or fuzzy matching. McKesson has both of these and they
        mean different things."""
        assert normalize_header("Ordered Quantity") != normalize_header("Order Quantity")
        assert normalize_header("Total Ordered Quantity") != normalize_header("Ordered Quantity")

    def test_none_and_empty(self):
        assert normalize_header(None) == ""
        assert normalize_header("") == ""


class TestJunkHeaders:
    @pytest.mark.parametrize(
        "raw", ["Unnamed: 0", "Unnamed: 12", "Textbox42", "Textbox", "", "   "]
    )
    def test_export_artifacts_are_junk(self, raw):
        assert is_junk_header(raw)

    @pytest.mark.parametrize("raw", ["NDC", "Account #", "Column Name", "Textbox Notes"])
    def test_real_columns_are_not_junk(self, raw):
        assert not is_junk_header(raw)

    def test_junk_is_excluded_from_matching_but_kept_for_the_record(self):
        idx = HeaderIndex(["Unnamed: 0", "NDC", "Textbox3"])
        assert len(idx) == 1
        assert idx.junk_headers == ("Unnamed: 0", "Textbox3")
        assert idx.headers == ("Unnamed: 0", "NDC", "Textbox3")


class TestMatching:
    def test_finds_a_column_by_its_current_name(self):
        idx = HeaderIndex(CARDINAL_DETAIL)
        match = idx.find("ndc", ["NDC"])
        assert match.source_header == "NDC"
        assert match.position == 10
        assert match.matched_alias == "NDC"

    def test_alias_order_is_preference_order(self):
        """A spec lists the current header first and older ones after, so a
        file carrying both binds to the current one."""
        idx = HeaderIndex(["LOCATION_NAME", "LOCATION_NAME1"])
        assert (
            idx.find("loc", ["LOCATION_NAME1", "LOCATION_NAME"]).source_header == "LOCATION_NAME1"
        )
        assert idx.find("loc", ["LOCATION_NAME", "LOCATION_NAME1"]).source_header == "LOCATION_NAME"

    def test_falls_through_to_a_later_alias(self):
        idx = HeaderIndex(["LOCATION_NAME"])
        match = idx.find("loc", ["LOCATION_NAME1", "LOCATION_NAME"])
        assert match.source_header == "LOCATION_NAME"
        assert match.matched_alias == "LOCATION_NAME"

    def test_returns_none_when_no_alias_matches(self):
        assert HeaderIndex(CARDINAL_DETAIL).find("nope", ["Not A Column"]) is None

    def test_matching_is_punctuation_insensitive(self):
        idx = HeaderIndex(["Purchase Dollars (Exclude Taxes, Fees & Surcharges)"])
        assert idx.find("amt", ["purchase dollars exclude taxes fees surcharges"]) is not None

    def test_membership_check(self):
        idx = HeaderIndex(CARDINAL_DETAIL)
        assert "Ship Qty" in idx
        assert "ship qty" in idx
        assert "Nonexistent" not in idx


class TestAmbiguity:
    def test_duplicate_columns_are_rejected_at_construction(self):
        """Two columns that normalize alike cannot be told apart, so the load
        stops rather than binding to whichever came first."""
        with pytest.raises(AmbiguousHeaderError, match="indistinguishable"):
            HeaderIndex(["Ship Qty", "SHIP QTY"])

    def test_the_error_names_the_offending_columns_and_positions(self):
        with pytest.raises(AmbiguousHeaderError) as excinfo:
            HeaderIndex(["NDC", "Trade Name", "ndc"])
        message = str(excinfo.value)
        assert "'ndc'" in message
        assert "[0, 2]" in message

    def test_repeated_junk_columns_do_not_trigger_ambiguity(self):
        """pandas emits Unnamed: 0, Unnamed: 1 ... and an SSRS export emits
        several Textbox columns; neither is a real collision."""
        HeaderIndex(["Unnamed: 0", "Unnamed: 1", "NDC"])
        HeaderIndex(["Textbox1", "Textbox2", "NDC"])


class TestVariantDetection:
    def test_fingerprints_distinguish_the_two_cardinal_reports(self):
        """The 3-year Cardinal history is the 9-column invoice summary with no
        NDC; later pulls are the 17-column detail report. Both are legitimate
        Cardinal files and the spec has to tell them apart."""
        detail = HeaderIndex(CARDINAL_DETAIL)
        summary = HeaderIndex(CARDINAL_SUMMARY)
        assert detail.fingerprint() != summary.fingerprint()
        assert summary.fingerprint() < detail.fingerprint()

    def test_detail_variant_is_identified_by_its_marker_columns(self):
        detail_markers = ["NDC", "Invoice Line #", "Ship Qty"]
        assert HeaderIndex(CARDINAL_DETAIL).has_all(detail_markers)
        assert not HeaderIndex(CARDINAL_SUMMARY).has_all(detail_markers)

    def test_summary_variant_is_identified_by_absence(self):
        assert not HeaderIndex(CARDINAL_SUMMARY).has_any(["NDC"])
        assert HeaderIndex(CARDINAL_DETAIL).has_any(["NDC"])


class TestDriftDetection:
    def test_unclaimed_columns_are_reported(self):
        """A vendor adding a column must surface, not vanish. The previous
        pipeline dropped these silently."""
        idx = HeaderIndex([*CARDINAL_DETAIL, "New Vendor Column"])
        claimed = set(range(len(CARDINAL_DETAIL)))
        assert idx.unmatched_headers(claimed) == {"New Vendor Column"}

    def test_no_drift_when_every_column_is_claimed(self):
        idx = HeaderIndex(CARDINAL_DETAIL)
        assert idx.unmatched_headers(set(range(len(CARDINAL_DETAIL)))) == set()

    def test_junk_columns_are_not_reported_as_drift(self):
        idx = HeaderIndex(["Unnamed: 0", "NDC"])
        ndc_position = idx.find("ndc", ["NDC"]).position
        assert idx.unmatched_headers({ndc_position}) == set()
