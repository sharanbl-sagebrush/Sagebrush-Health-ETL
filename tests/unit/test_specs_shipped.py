"""Every shipped spec, checked against the real header rows it must handle.

Headers are copied verbatim from the files rather than read from disk, so the
suite runs in CI without the extracts and without touching PHI. A separate
integration check (``scripts/bind_real_files.py``) binds against the actual
files when they are available.

If a vendor changes a header, the copy here goes stale and the corresponding
integration run fails - which is the point. This file is the contract; the
integration run is the evidence it still holds.

How to read this file:

- TestAllSpecsAreSound  rules every one of the 20 specs must satisfy, found by
                        iterating the registry rather than naming specs
- TestHandOff           a recognise_only variant names its reason, and the
                        two Cardinal specs hand each other's report over
- TestMasterCogsList    the account register: two variants in one workbook
- TestMonthlyPayments   the 116-column PM ledger: PII classes, SSN, period
- TestInjectedColumns   ``source_sheet``, a column the loader supplies
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
    """Every YAML file under specs/, loaded and validated once."""
    return SpecRegistry.from_dir(SPEC_DIR)


# --- real header rows, verbatim -------------------------------------------

# Master COGS List, "Master Sheet" tab: the consolidated register with the
# Class_New and Changed? columns that record the undated reclassification.
MASTER_COGS_MASTER_SHEET = [
    "Payee", "Class", "Class_New", "Changed?", "Category", "Account No", "Location",
]  # fmt: skip

# Master COGS List, one of the per-vendor tabs (Cardinal, ABC, ...): five
# columns, no Class_New.
MASTER_COGS_VENDOR_TAB = [
    "Payee", "Class", "Category", "Account No", "Location",
]  # fmt: skip

# The R&S tab spells the account header with a lower-case n.
MASTER_COGS_RS_TAB = [
    "Payee", "Class", "Category", "Account no", "Location",
]  # fmt: skip

# Monthly Payments: the full 116-column header, stable across all 40 monthly
# files. Includes SSN (never landed) and the direct identifiers that land
# encrypted.
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
    """Cross-cutting rules, applied to every spec in the registry. Adding a
    spec adds it to every one of these tests automatically."""

    def test_every_spec_loads_and_keys_are_unique(self, registry):
        """Deliberately not a fixed list. Asserting the exact set of shipped
        specs means every new source breaks an unrelated test, which trains
        people to update the expectation without reading it."""
        keys = [s.key for s in registry]
        assert keys, "no specs loaded"
        assert len(keys) == len(set(keys))
        assert keys == sorted(keys)

    def test_the_sources_this_module_asserts_on_are_present(self, registry):
        """The three specs the later classes test by name must exist, so a
        rename fails here with a clear message rather than in a KeyError."""
        for key in (
            "finance.monthly_payments",
            "reference.wholesaler_accounts",
            "wholesaler.cardinal.invoice",
        ):
            assert registry.get(key) is not None, key

    def test_no_spec_leaves_a_date_column_to_inference(self, registry):
        """Every date column in every variant declares at least one format.
        The model enforces this too; this is the belt to that brace."""
        for spec in registry:
            for variant in spec.variants:
                for column in variant.columns:
                    if column.type is FieldType.DATE:
                        assert column.date_formats, f"{spec.key}.{column.target}"

    def test_every_natural_key_column_is_declared(self, registry):
        """A natural-key column must be a target in every variant, or a file
        of that variant would land without a key."""
        for spec in registry:
            for variant in spec.variants:
                targets = {c.target for c in variant.columns}
                assert set(spec.natural_key) <= targets, spec.key

    def test_control_totals_are_only_on_numeric_columns(self, registry):
        """Summing text is meaningless, and a meaningless control total is worse
        than none because somebody will try to reconcile against it."""
        numeric = {FieldType.CURRENCY, FieldType.DECIMAL, FieldType.INTEGER}
        for spec in registry:
            for variant in spec.variants:
                for column in variant.columns:
                    if column.control_total:
                        assert column.type in numeric, f"{spec.key}.{column.target}"

    def test_a_prohibited_column_is_never_part_of_a_key(self, registry):
        """A value that never lands cannot identify a row."""
        for spec in registry:
            for variant in spec.variants:
                for column in variant.columns:
                    if column.pii is PiiClass.PROHIBITED:
                        assert column.target not in spec.natural_key, spec.key

    def test_a_prohibited_column_is_never_required(self, registry):
        """Required means the value is checked; a prohibited value must not
        be examined at all beyond locating the column."""
        for spec in registry:
            for variant in spec.variants:
                for column in variant.columns:
                    if column.pii is PiiClass.PROHIBITED:
                        assert not column.required, f"{spec.key}.{column.target}"

    def test_every_spec_has_at_least_one_loading_variant(self, registry):
        """A spec whose variants are all recognise_only would never land
        anything; that is a mistake, not a source."""
        for spec in registry:
            assert any(v.loads for v in spec.variants), spec.key

    def test_a_recognise_only_variant_names_exactly_one_reason(self):
        """``superseded_by`` (a sibling) or ``loaded_by`` (another source),
        never both and never neither; a loading variant has neither."""
        from sbh_data_bank.registry.spec import SourceSpec

        def build(**other):
            return SourceSpec.model_validate(
                {
                    "key": "a.b",
                    "name": "n",
                    "vendor": "v",
                    "natural_key": ["id"],
                    "variants": [
                        {
                            "name": "main",
                            "detect": {"require_all": ["ID"]},
                            "columns": [{"target": "id", "sources": ["ID"], "required": True}],
                        },
                        {
                            "name": "other",
                            "detect": {"require_all": ["X"]},
                            "columns": [{"target": "id", "sources": ["ID"]}],
                            **other,
                        },
                    ],
                }
            )

        assert build(action="recognise_only", loaded_by="c.d").variants[1].loaded_by == "c.d"
        assert build(action="recognise_only", superseded_by="main").variants[1].loads is False
        with pytest.raises(ValueError, match="must name either"):
            build(action="recognise_only")
        with pytest.raises(ValueError, match="must name either"):
            build(action="recognise_only", superseded_by="main", loaded_by="c.d")
        with pytest.raises(ValueError, match="cannot be superseded or loaded elsewhere"):
            build(loaded_by="c.d")
        with pytest.raises(ValueError, match="loaded by this very source"):
            build(action="recognise_only", loaded_by="a.b")

    def test_a_hand_off_to_a_source_that_does_not_exist_is_refused(self, tmp_path):
        """The registry, not the spec, knows which keys exist."""
        import yaml

        from sbh_data_bank.registry.loader import SpecLoadError, load_spec_dir

        (tmp_path / "a.b.yaml").write_text(
            yaml.safe_dump(
                {
                    "key": "a.b",
                    "name": "n",
                    "vendor": "v",
                    "natural_key": ["id"],
                    "variants": [
                        {
                            "name": "main",
                            "detect": {"require_all": ["ID"]},
                            "columns": [{"target": "id", "sources": ["ID"], "required": True}],
                        },
                        {
                            "name": "other",
                            "detect": {"require_all": ["X"]},
                            "action": "recognise_only",
                            "loaded_by": "nobody.home",
                            "columns": [{"target": "id", "sources": ["ID"]}],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(SpecLoadError, match="'nobody.home', which is not a spec"):
            load_spec_dir(tmp_path)

    def test_the_two_cardinal_specs_hand_each_other_over(self, registry):
        detail = registry.require("wholesaler.cardinal.invoice")
        summary = registry.require("wholesaler.cardinal.invoice_summary")
        by_name = {v.name: v for v in detail.variants}
        assert by_name["invoice_summary"].loaded_by == summary.key
        by_name = {v.name: v for v in summary.variants}
        assert by_name["invoice_detail"].loaded_by == detail.key
        assert detail.filename_patterns == summary.filename_patterns

    def test_max_length_stays_within_what_nvarchar_allows(self):
        from sbh_data_bank.registry.spec import ColumnSpec

        assert ColumnSpec(target="n", sources=["N"], max_length=4000).max_length == 4000
        with pytest.raises(ValueError, match="between 1 and 4000"):
            ColumnSpec(target="n", sources=["N"], max_length=4001)
        with pytest.raises(ValueError, match="between 1 and 4000"):
            ColumnSpec(target="n", sources=["N"], max_length=0)

    def test_a_keyless_spec_is_rejected_without_notes(self):
        """The rule itself: an empty natural key must be justified."""
        from sbh_data_bank.registry.spec import SourceSpec

        with pytest.raises(ValueError, match="notes must record"):
            SourceSpec.model_validate(
                {
                    "key": "a.b",
                    "name": "n",
                    "vendor": "v",
                    "variants": [
                        {
                            "name": "v1",
                            "detect": {"require_all": ["A"]},
                            "columns": [{"target": "a", "sources": ["A"]}],
                        }
                    ],
                }
            )

    def test_no_detector_accepts_another_specs_file(self, registry):
        """Across specs, detectors must be mutually exclusive on header content.

        Each loading variant's declared source headers stand in for a real file
        of that shape. Routing by filename is only a hint, so if another spec's
        detector is satisfied by those headers, a renamed or misdropped file
        would bind as the wrong source. The two McKesson reports share 15 of
        their headers and are the case that motivated this check.

        The one sanctioned exception is a declared hand-off: a recognise_only
        variant whose ``loaded_by`` names the owner accepts the owner's shape
        on purpose (the two Cardinal specs, each naming the other).
        """
        shapes = []
        for spec in registry:
            for variant in spec.variants:
                headers = HeaderIndex([c.sources[0] for c in variant.columns if c.sources])
                shapes.append((spec.key, variant.name, headers))
        accepted = []
        for owner_key, variant_name, headers in shapes:
            for other in registry:
                if other.key == owner_key:
                    continue
                for candidate in other.variants:
                    if candidate.loaded_by == owner_key:
                        continue
                    missing = [h for h in candidate.detect.require_all if h not in headers]
                    forbidden = [h for h in candidate.detect.require_none if h in headers]
                    if not missing and not forbidden:
                        accepted.append(
                            f"{other.key}/{candidate.name} accepts {owner_key}/{variant_name}"
                        )
        assert not accepted, chr(10).join(accepted)

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("WeInfuse AdministeredDrugsReport Jan012025-Feb052026.csv",
             {"dispense.weinfuse.administered"}),
            ("WeInfuse_AdministeredDrugsReport 03012026-06102026.xlsx",
             {"dispense.weinfuse.administered"}),
            ("WeInfuse_BuyBill-AdministeredDrugsReport 01012023-03062026.csv",
             {"dispense.weinfuse.buybill"}),
            ("WeInfuse_BuyBill-AdministeredDrugsReport Jan012025-Feb052026.csv",
             {"dispense.weinfuse.buybill"}),
            ("Athena_Patient Eligibility_Feb042026.csv", {"ehr.athena.eligibility"}),
        ],
    )  # fmt: skip
    def test_real_filenames_route_to_exactly_one_spec(self, registry, filename, expected):
        """The filename is a routing hint, but a hint that sends a file to two
        specs costs a recorded UNRECOGNISED attempt on the wrong one every
        time. The backfill dry run (2026-10-21) found every BuyBill file
        routed to the administered spec as well."""
        assert {s.key for s in registry if s.matches_filename(filename)} == expected

    def test_a_keyless_spec_documents_why(self, registry):
        """An empty natural key is legitimate - several of these feeds are
        denormalised report extracts where no subset of columns identifies a
        row - but it changes row identity to the row hash, so it has to be a
        recorded finding rather than an omission."""
        for spec in registry:
            if not spec.natural_key:
                assert spec.notes.strip(), (
                    f"{spec.key} has no natural key and no notes explaining it"
                )


class TestMasterCogsList:
    """``reference.wholesaler_accounts``: the finance workbook that maps each
    wholesaler account to a site. It is the source for entity resolution,
    which is why its key and typing rules matter more than its 121 rows
    suggest (docs/data-findings.md finding 6)."""

    def test_the_master_sheet_binds(self, registry):
        """The seven-column Master Sheet header binds cleanly to its variant."""
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
    """``finance.monthly_payments``: the practice-management transaction
    ledger. 1,287,742 rows across 40 monthly files, the highest-PHI file in
    the estate, and the one that carried the $39.97M defect (findings 1 and 7)."""

    def test_the_full_116_column_header_binds_with_no_drift(self, registry):
        """All 116 real columns bind: nothing missing, nothing extra."""
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
        """A payment line with no amount is not a payment line; quarantine it."""
        variant = registry.require("finance.monthly_payments").variants[0]
        assert variant.column("amount").required

    def test_the_three_date_formats_are_declared(self, registry):
        """One file, three date shapes: TransDate 6/17/2025, TransMonth
        "Jun 2025", PostMonth "Oct 2025" (finding 7). Each column declares
        its own."""
        variant = registry.require("finance.monthly_payments").variants[0]
        assert "%m/%d/%Y" in variant.column("transdate").date_formats
        assert "%b %Y" in variant.column("transmonth").date_formats
        assert "%b %Y" in variant.column("postmonth").date_formats

    def test_direct_identifiers_are_classified(self, registry):
        """``pii: direct`` is what will drive encryption at rest and
        column-level access control for name, DOB, contact and address."""
        variant = registry.require("finance.monthly_payments").variants[0]
        for target in ("lname", "fname", "dob", "email", "cellphone", "patstreet1"):
            assert variant.column(target).pii is PiiClass.DIRECT, target

    def test_join_keys_stay_in_clear(self, registry):
        """PatNum, MRN and ClientID are needed to join to other sources, so
        they are classified join_key and stored in clear (decision in
        finding 7)."""
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
        """The pattern is specific enough not to match another source's name."""
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
    """A column with no ``sources``, filled in by the loader rather than read
    from the file. The one case today is ``source_sheet`` on the Master COGS
    List, which records which worksheet a row came from."""

    def test_an_injected_column_is_never_reported_as_absent(self, registry):
        """source_sheet has no vendor header - the loader supplies it - so it
        is neither matched nor missing, and must not appear as drift."""
        spec = registry.require("reference.wholesaler_accounts")
        binding = bind(spec, HeaderIndex(MASTER_COGS_MASTER_SHEET))
        assert "source_sheet" not in binding.missing_optional
        assert "source_sheet" not in binding.missing_required
        assert binding.drift_report() == "no drift"

    def test_an_injected_column_counts_as_present_and_landed(self, registry):
        """It has no header match (``match is None``) yet it is present and
        will be written, because the loader fills it."""
        spec = registry.require("reference.wholesaler_accounts")
        binding = bind(spec, HeaderIndex(MASTER_COGS_MASTER_SHEET))
        injected = next(b for b in binding.columns if b.column.target == "source_sheet")
        assert injected.is_injected
        assert injected.is_present
        assert injected.match is None
        assert injected in binding.landed

    def test_an_injected_column_is_never_flagged_as_renamed(self, registry):
        """No aliases means no "bound via a later alias" warning either."""
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
                    "natural_key": ["a"],
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
