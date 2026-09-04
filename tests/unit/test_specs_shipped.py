"""Every shipped spec, checked against the real header rows it must handle.

Headers are copied verbatim from the files rather than read from disk, so the
suite runs in CI without the extracts and without touching PHI. A separate
integration check (``scripts/bind_real_files.py``) binds against the actual
files when they are available.

If a vendor changes a header, the copy here goes stale and the corresponding
integration run fails - which is the point. This file is the contract; the
integration run is the evidence it still holds.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sbh_data_bank.registry.binding import bind
from sbh_data_bank.registry.headers import HeaderIndex
from sbh_data_bank.registry.loader import SpecRegistry
from sbh_data_bank.registry.spec import FieldType, PiiClass

SPEC_DIR = Path(__file__).resolve().parents[2] / "specs"


@pytest.fixture(scope="module")
def registry() -> SpecRegistry:
    return SpecRegistry.from_dir(SPEC_DIR)


# --- real header rows, verbatim -------------------------------------------

MASTER_COGS_MASTER_SHEET = [
    "Payee", "Class", "Class_New", "Changed?", "Category", "Account No", "Location",
]  # fmt: skip

MASTER_COGS_VENDOR_TAB = [
    "Payee", "Class", "Category", "Account No", "Location",
]  # fmt: skip

# The R&S tab spells the account header with a lower-case n.
MASTER_COGS_RS_TAB = [
    "Payee", "Class", "Category", "Account no", "Location",
]  # fmt: skip

MONTHLY_PAYMENTS = [
    "ClientID", "Voucher_Number", "Service_ID", "SeqNum", "IsVoided", "VoidSign",
    "VoidFlag", "TransCatID", "TransCat", "TransCatDesc", "TransType", "TransCodeID",
    "TransCode", "TransDesc", "Amount", "TransDate", "TransMonth", "Date_Paid",
    "ReimbCatID", "ReimbCat", "ReimbCatDesc", "ReimbCommentID", "ReimbComment",
    "ReimbCmtDesc", "InsCatRemID", "InsCatRem", "InsCatRemName", "CarrierRemID",
    "CarrierRem", "CarrierRemName", "InsCatRemCurID", "InsCatRemCur",
    "InsCatRemCurName", "CarrierRemCurID", "CarrierRemCur", "CarrierRemCurName",
    "InsCatID", "InsCat", "InsCatName", "CarrierID", "Carrier", "CarrierName",
    "InsCatOrigID", "InsCatOrig", "InsCatOrigName", "CarrierOrigID", "CarrierOrig",
    "CarrierOrigName", "Reference", "PatNum", "MRN", "LName", "FName", "MI", "DOB",
    "Gender", "MaritalStatus", "SSN", "Phone", "WorkPhone", "CellPhone", "Email",
    "PatStreet1", "PatStreet2", "PatCity", "PatState", "PatZipCode", "PostMonth",
    "SvcDate", "SvcDateFrom", "SvcDateTo", "ActProvID", "ActProv", "ActProvName",
    "ActProvSuffix", "BillProvID", "BillProv", "BillProvName", "BillProvSuffix",
    "RefProvID", "RefProv", "RefProvName", "RefProvSuffix", "DivisionID", "Division",
    "DivisionName", "DeptID", "Dept", "DeptDesc", "PlcSvcID", "PlcSvc", "PlcSvcDesc",
    "VisitLoc", "VisitLocAbbr", "VisitLocDesc", "ProcCatID", "ProcCat", "ProcCatName",
    "ProcCode", "ProcDesc", "BillingCode", "Modifiers", "DxCode1", "DxDesc1",
    "DxCode2", "DxDesc2", "DxCode3", "DxDesc3", "DxCode4", "DxDesc4", "Fee", "Units",
    "OperatorName", "BatchCategory", "BatchNum", "WorkRVU",
]  # fmt: skip


class TestAllSpecsAreSound:
    def test_every_spec_loads_and_keys_are_unique(self, registry):
        assert len(registry) == 3
        assert [s.key for s in registry] == [
            "finance.monthly_payments",
            "reference.wholesaler_accounts",
            "wholesaler.cardinal.invoice",
        ]

    def test_no_spec_leaves_a_date_column_to_inference(self, registry):
        for spec in registry:
            for variant in spec.variants:
                for column in variant.columns:
                    if column.type is FieldType.DATE:
                        assert column.date_formats, f"{spec.key}.{column.target}"

    def test_every_natural_key_column_is_declared(self, registry):
        for spec in registry:
            for variant in spec.variants:
                targets = {c.target for c in variant.columns}
                assert set(spec.natural_key) <= targets, spec.key

    def test_no_spec_has_an_empty_natural_key(self, registry):
        """An empty key means append-only with no dedup. Legitimate for some
        sources, but none of these three, so assert it deliberately."""
        for spec in registry:
            assert spec.natural_key, spec.key


class TestMasterCogsList:
    def test_the_master_sheet_binds(self, registry):
        spec = registry.require("reference.wholesaler_accounts")
        binding = bind(spec, HeaderIndex(MASTER_COGS_MASTER_SHEET))
        assert binding.variant.name == "master_sheet"
        assert binding.missing_required == ()
        assert binding.new_columns == ()

    def test_a_vendor_tab_binds_to_the_other_variant(self, registry):
        """Both are legitimately this workbook; only the presence of Class_New
        separates the authoritative consolidated sheet from a working copy."""
        spec = registry.require("reference.wholesaler_accounts")
        binding = bind(spec, HeaderIndex(MASTER_COGS_VENDOR_TAB))
        assert binding.variant.name == "vendor_tab"
        assert binding.missing_required == ()

    def test_the_rs_tab_lower_case_account_header_binds(self, registry):
        """The R&S tab spells it "Account no"; every other tab uses "Account No"."""
        spec = registry.require("reference.wholesaler_accounts")
        binding = bind(spec, HeaderIndex(MASTER_COGS_RS_TAB))
        account = next(b for b in binding.columns if b.column.target == "account_number")
        assert account.match.source_header == "Account no"

    def test_account_number_is_text_so_leading_zeros_survive(self, registry):
        """ABC accounts appear as 100583154 here and 0100583154 in the
        wholesaler extracts. An integer conversion would destroy the
        distinction and make the two impossible to reconcile deliberately."""
        spec = registry.require("reference.wholesaler_accounts")
        for variant in spec.variants:
            assert variant.column("account_number").type is FieldType.TEXT

    def test_location_is_required_because_it_disambiguates_reused_accounts(self, registry):
        """McKesson 62766 maps to three different classes; only Location tells
        them apart, so it cannot be optional."""
        spec = registry.require("reference.wholesaler_accounts")
        assert "location" in spec.natural_key
        for variant in spec.variants:
            assert variant.column("location").required

    def test_both_classifications_land_uninterpreted(self, registry):
        """Decision of 2026-09-04: no effective date was recorded for the
        Class -> Class_New remapping, so both are stored and neither is
        treated as current."""
        master = registry.require("reference.wholesaler_accounts").variant("master_sheet")
        assert master.column("class_current") is not None
        assert master.column("class_proposed") is not None


class TestMonthlyPayments:
    def test_the_full_116_column_header_binds_with_no_drift(self, registry):
        spec = registry.require("finance.monthly_payments")
        binding = bind(spec, HeaderIndex(MONTHLY_PAYMENTS))
        assert binding.missing_required == ()
        assert binding.missing_optional == ()
        assert binding.new_columns == ()
        assert len(binding.present) == 116

    def test_ssn_is_matched_but_never_landed(self, registry):
        """Matched so drift and column-count checks still see it; discarded so
        no value reaches the raw layer, quarantine or a log."""
        spec = registry.require("finance.monthly_payments")
        binding = bind(spec, HeaderIndex(MONTHLY_PAYMENTS))
        assert [b.column.target for b in binding.prohibited] == ["ssn"]
        assert len(binding.landed) == 115
        assert "ssn" not in {b.column.target for b in binding.landed}

    def test_money_columns_are_currency_not_decimal(self, registry):
        """type: currency is what handles ($40.00). Getting this wrong on
        Amount is the $39.97M defect."""
        variant = registry.require("finance.monthly_payments").variants[0]
        assert variant.column("amount").type is FieldType.CURRENCY
        assert variant.column("fee").type is FieldType.CURRENCY

    def test_amount_is_required(self, registry):
        variant = registry.require("finance.monthly_payments").variants[0]
        assert variant.column("amount").required

    def test_the_three_date_formats_are_declared(self, registry):
        variant = registry.require("finance.monthly_payments").variants[0]
        assert "%m/%d/%Y" in variant.column("transdate").date_formats
        assert "%b %Y" in variant.column("transmonth").date_formats
        assert "%b %Y" in variant.column("postmonth").date_formats

    def test_direct_identifiers_are_classified(self, registry):
        variant = registry.require("finance.monthly_payments").variants[0]
        for target in ("lname", "fname", "dob", "email", "cellphone", "patstreet1"):
            assert variant.column(target).pii is PiiClass.DIRECT, target

    def test_join_keys_stay_in_clear(self, registry):
        variant = registry.require("finance.monthly_payments").variants[0]
        for target in ("patnum", "mrn", "clientid"):
            assert variant.column(target).pii is PiiClass.JOIN_KEY, target

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("Monthly Payments 2025-06.csv", {"year": "2025", "month": "06"}),
            ("Monthly Payments 2026-07 upto 07212026.csv", {"year": "2026", "month": "07"}),
            ("Monthly Payments 2023-04.csv", {"year": "2023", "month": "04"}),
        ],
    )
    def test_period_is_recoverable_from_the_filename(self, registry, filename, expected):
        """One file per month, including the partial-period form that uses a
        different naming convention."""
        spec = registry.require("finance.monthly_payments")
        match = re.search(spec.period_from_filename, filename, re.IGNORECASE)
        assert match is not None
        assert match.groupdict() == expected

    def test_a_non_payments_filename_yields_no_period(self, registry):
        spec = registry.require("finance.monthly_payments")
        assert re.search(spec.period_from_filename, "Cardinal_Invoice.csv", re.I) is None

    def test_a_vendor_adding_a_column_blocks_this_source(self, registry):
        """116 columns is a wide, stable contract; a change on either side is
        worth stopping for."""
        spec = registry.require("finance.monthly_payments")
        binding = bind(spec, HeaderIndex([*MONTHLY_PAYMENTS, "NewColumn"]))
        assert binding.new_columns == ("NewColumn",)
        assert binding.blocking_problem() is not None


class TestInjectedColumns:
    def test_an_injected_column_is_never_reported_as_absent(self, registry):
        """source_sheet has no vendor header - the loader supplies it - so it
        is neither matched nor missing, and must not appear as drift."""
        spec = registry.require("reference.wholesaler_accounts")
        binding = bind(spec, HeaderIndex(MASTER_COGS_MASTER_SHEET))
        assert "source_sheet" not in binding.missing_optional
        assert "source_sheet" not in binding.missing_required
        assert binding.drift_report() == "no drift"

    def test_an_injected_column_counts_as_present_and_landed(self, registry):
        spec = registry.require("reference.wholesaler_accounts")
        binding = bind(spec, HeaderIndex(MASTER_COGS_MASTER_SHEET))
        injected = next(b for b in binding.columns if b.column.target == "source_sheet")
        assert injected.is_injected
        assert injected.is_present
        assert injected.match is None
        assert injected in binding.landed

    def test_an_injected_column_is_never_flagged_as_renamed(self, registry):
        spec = registry.require("reference.wholesaler_accounts")
        binding = bind(spec, HeaderIndex(MASTER_COGS_VENDOR_TAB))
        assert binding.renamed == ()

    def test_only_the_declared_injected_column_may_lack_sources(self):
        """Guards the escape hatch: an ordinary column left sourceless by
        accident would silently land null forever."""
        from sbh_data_bank.registry.spec import SourceSpec

        with pytest.raises(ValueError, match="declare no sources"):
            SourceSpec.model_validate(
                {
                    "key": "a.b",
                    "name": "n",
                    "vendor": "v",
                    "variants": [
                        {
                            "name": "v1",
                            "detect": {"require_all": ["A"]},
                            "columns": [
                                {"target": "a", "sources": ["A"]},
                                {"target": "oops"},
                            ],
                        }
                    ],
                }
            )
