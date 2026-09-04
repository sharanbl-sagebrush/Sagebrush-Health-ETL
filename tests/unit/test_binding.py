"""Tests for spec loading, variant selection and column binding.

The Cardinal spec is exercised against the exact header rows of all four
Cardinal files in the estate, so the suite fails if a real file stops binding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sbh_data_bank.registry.binding import (
    AmbiguousVariantError,
    NoVariantMatchedError,
    bind,
    select_variant,
)
from sbh_data_bank.registry.headers import HeaderIndex
from sbh_data_bank.registry.loader import SpecLoadError, SpecRegistry, load_spec
from sbh_data_bank.registry.spec import (
    ColumnSpec,
    DriftPolicy,
    FieldType,
    PiiClass,
    SchemaVariant,
    SourceSpec,
    VariantDetector,
)

SPEC_DIR = Path(__file__).resolve().parents[2] / "specs"

# Header rows exactly as they appear in the files.
CARDINAL_DETAIL = [
    "Account #", "Account Name", "Account Alias", "PO #", "Invoice #",
    "Invoice Line #", "Invoice Date", "Invoice Type", "Purchase/Return", "CIN",
    "NDC", "Trade Name", "Generic Name", "Unit price (Current Invoice)",
    "Original Ordered Qty", "Ship Qty",
    "Purchase Dollars (Exclude Taxes, Fees & Surcharges)",
]  # fmt: skip

CARDINAL_SUMMARY = [
    "Account #", "Account Name", "Account Alias", "PO #", "Invoice #",
    "Invoice Date", "Invoice Type", "Purchase/Return",
    "Purchase Dollars (Exclude Taxes, Fees & Surcharges)",
]  # fmt: skip


@pytest.fixture(scope="module")
def cardinal() -> SourceSpec:
    return load_spec(SPEC_DIR / "wholesaler.cardinal.invoice.yaml")


class TestSpecLoading:
    def test_the_cardinal_spec_loads(self, cardinal):
        assert cardinal.key == "wholesaler.cardinal.invoice"
        assert [v.name for v in cardinal.variants] == ["invoice_detail", "invoice_summary"]

    def test_every_shipped_spec_is_valid(self):
        """Guards against a spec being committed that cannot load."""
        registry = SpecRegistry.from_dir(SPEC_DIR)
        assert len(registry) >= 1

    def test_a_typo_in_a_spec_key_is_a_failure_not_a_silent_default(self, tmp_path):
        """The previous settings.yaml had duplicated keys where the second
        silently won. extra=forbid makes that impossible."""
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "key: a.b\nname: x\nvendor: y\nvaraints: []\n"  # note the typo
            "variants:\n  - name: v\n    detect: {require_all: [A]}\n"
            "    columns:\n      - {target: a, sources: [A]}\n",
            encoding="utf-8",
        )
        with pytest.raises(SpecLoadError, match="varaints"):
            load_spec(bad)


class TestSpecValidation:
    def _minimal(self, **overrides):
        base = {
            "key": "a.b",
            "name": "n",
            "vendor": "v",
            "natural_key": ["a"],
            "variants": [
                {
                    "name": "only",
                    "detect": {"require_all": ["A"]},
                    "columns": [{"target": "a", "sources": ["A"]}],
                }
            ],
        }
        return {**base, **overrides}

    def test_a_date_column_must_declare_formats(self):
        with pytest.raises(ValueError, match="must declare date_formats"):
            ColumnSpec(target="d", sources=["D"], type=FieldType.DATE)

    def test_a_non_date_column_cannot_declare_formats(self):
        with pytest.raises(ValueError, match="cannot have date_formats"):
            ColumnSpec(target="t", sources=["T"], type=FieldType.TEXT, date_formats=["%Y"])

    def test_a_detector_must_assert_something(self):
        with pytest.raises(ValueError, match="at least one column"):
            VariantDetector()

    def test_a_variant_cannot_map_the_same_target_twice(self):
        with pytest.raises(ValueError, match="more than once"):
            SchemaVariant(
                name="v",
                detect=VariantDetector(require_all=["A"]),
                columns=[
                    ColumnSpec(target="a", sources=["A"]),
                    ColumnSpec(target="a", sources=["B"]),
                ],
            )

    def test_natural_key_columns_must_exist_in_every_variant(self):
        """Otherwise a file of the wrong shape lands keyless and silently
        duplicates on re-delivery."""
        with pytest.raises(ValueError, match="lacks natural-key columns"):
            SourceSpec.model_validate(self._minimal(natural_key=["missing_col"]))

    def test_a_constant_cannot_overwrite_a_mapped_column(self):
        with pytest.raises(ValueError, match="constants would overwrite"):
            SourceSpec.model_validate(self._minimal(constants={"a": "x"}))

    def test_period_pattern_needs_year_and_month_groups(self):
        with pytest.raises(ValueError, match="named groups"):
            SourceSpec.model_validate(self._minimal(period_from_filename=r"(\d{4})"))

    def test_variant_names_must_be_unique(self):
        spec = self._minimal()
        spec["variants"] = spec["variants"] * 2
        with pytest.raises(ValueError, match="variant names must be unique"):
            SourceSpec.model_validate(spec)


class TestVariantSelection:
    def test_the_detail_report_selects_the_detail_variant(self, cardinal):
        assert select_variant(cardinal, HeaderIndex(CARDINAL_DETAIL)).name == "invoice_detail"

    def test_the_summary_report_selects_the_summary_variant(self, cardinal):
        """This is the case that matters. Both files are legitimately Cardinal
        invoices; only the header set tells them apart, and reading the summary
        as the detail report would produce purchases with no NDC."""
        assert select_variant(cardinal, HeaderIndex(CARDINAL_SUMMARY)).name == "invoice_summary"

    def test_selection_ignores_the_filename_entirely(self, cardinal):
        """A Cardinal file named 01012023- contains no 2023 data, and the same
        source arrives as CSV in one drop and XLSX in the next."""
        assert select_variant(cardinal, HeaderIndex(CARDINAL_DETAIL)).name == "invoice_detail"

    def test_an_unrecognised_file_raises_with_a_usable_diagnosis(self, cardinal):
        with pytest.raises(NoVariantMatchedError) as excinfo:
            select_variant(cardinal, HeaderIndex(["Some", "Other", "Report"]))
        message = str(excinfo.value)
        assert "invoice_detail" in message and "invoice_summary" in message
        assert "missing" in message
        assert "file has 3 columns" in message

    def test_overlapping_detectors_are_a_spec_bug(self):
        spec = SourceSpec.model_validate(
            {
                "key": "a.b",
                "name": "n",
                "vendor": "v",
                "natural_key": ["a"],
                "variants": [
                    {
                        "name": "one",
                        "detect": {"require_all": ["A"]},
                        "columns": [{"target": "a", "sources": ["A"]}],
                    },
                    {
                        "name": "two",
                        "detect": {"require_all": ["A"]},
                        "columns": [{"target": "a", "sources": ["A"]}],
                    },
                ],
            }
        )
        with pytest.raises(AmbiguousVariantError, match="require_none"):
            select_variant(spec, HeaderIndex(["A"]))


class TestColumnBinding:
    def test_every_detail_column_binds(self, cardinal):
        binding = bind(cardinal, HeaderIndex(CARDINAL_DETAIL))
        assert binding.variant.name == "invoice_detail"
        assert binding.missing_required == ()
        assert binding.new_columns == ()
        assert len(binding.present) == 17

    def test_the_summary_binds_with_its_line_number_absent(self, cardinal):
        """invoice_line_number is part of the natural key but does not exist in
        the summary report. It is declared optional there and lands null."""
        binding = bind(cardinal, HeaderIndex(CARDINAL_SUMMARY))
        assert binding.variant.name == "invoice_summary"
        assert binding.missing_required == ()
        assert "invoice_line_number" in binding.missing_optional
        assert binding.blocking_problem() is None

    def test_ndc_binds_to_the_right_position(self, cardinal):
        binding = bind(cardinal, HeaderIndex(CARDINAL_DETAIL))
        ndc = next(b for b in binding.columns if b.column.target == "ndc")
        assert ndc.match.position == 10
        assert ndc.column.type is FieldType.NDC

    def test_the_punctuation_heavy_amount_column_binds(self, cardinal):
        binding = bind(cardinal, HeaderIndex(CARDINAL_DETAIL))
        amount = next(b for b in binding.columns if b.column.target == "purchase_amount")
        assert amount.match.source_header == "Purchase Dollars (Exclude Taxes, Fees & Surcharges)"
        assert amount.column.type is FieldType.CURRENCY


class TestDrift:
    def test_a_new_vendor_column_is_reported_not_dropped(self, cardinal):
        binding = bind(cardinal, HeaderIndex([*CARDINAL_DETAIL, "Rebate Amount"]))
        assert binding.new_columns == ("Rebate Amount",)
        assert binding.has_drift

    def test_a_new_column_only_warns_for_this_source(self, cardinal):
        """Cardinal's policy is on_new_column: warn, so the file still loads."""
        binding = bind(cardinal, HeaderIndex([*CARDINAL_DETAIL, "Rebate Amount"]))
        assert cardinal.on_new_column is DriftPolicy.WARN
        assert binding.blocking_problem() is None

    def test_a_missing_required_column_blocks_the_load(self, cardinal):
        headers = [h for h in CARDINAL_DETAIL if h != "Ship Qty"]
        # Removing Ship Qty also breaks the detail detector, so the file is
        # simply unrecognisable - which is the correct, louder outcome.
        with pytest.raises(NoVariantMatchedError):
            bind(cardinal, HeaderIndex(headers))

    def test_a_required_column_absent_but_still_detectable_blocks(self, cardinal):
        headers = [h for h in CARDINAL_DETAIL if h != "Purchase/Return"]
        binding = bind(cardinal, HeaderIndex(headers))
        assert "purchase_or_return" in binding.missing_required
        assert cardinal.on_missing_column is DriftPolicy.FAIL
        assert binding.blocking_problem() is not None

    def test_a_deprecated_alias_is_surfaced(self):
        spec = SourceSpec.model_validate(
            {
                "key": "a.b",
                "name": "n",
                "vendor": "v",
                "natural_key": ["loc"],
                "variants": [
                    {
                        "name": "v1",
                        "detect": {"require_all": ["LOCATION_NAME"]},
                        "columns": [
                            {"target": "loc", "sources": ["LOCATION_NAME1", "LOCATION_NAME"]}
                        ],
                    }
                ],
            }
        )
        binding = bind(spec, HeaderIndex(["LOCATION_NAME"]))
        assert binding.renamed
        assert "deprecated alias" in binding.drift_report()

    def test_drift_report_is_readable(self, cardinal):
        binding = bind(cardinal, HeaderIndex([*CARDINAL_DETAIL, "Rebate Amount"]))
        assert "Rebate Amount" in binding.drift_report()


class TestProhibitedColumns:
    def test_a_prohibited_column_is_matched_but_never_landed(self):
        """SSN must be seen, so drift and column-count checks still work, and
        then discarded without its value reaching anything that persists."""
        spec = SourceSpec.model_validate(
            {
                "key": "a.b",
                "name": "n",
                "vendor": "v",
                "natural_key": ["pat_num"],
                "variants": [
                    {
                        "name": "v1",
                        "detect": {"require_all": ["PatNum"]},
                        "columns": [
                            {"target": "pat_num", "sources": ["PatNum"], "pii": "join_key"},
                            {"target": "ssn", "sources": ["SSN"], "pii": "prohibited"},
                        ],
                    }
                ],
            }
        )
        binding = bind(spec, HeaderIndex(["PatNum", "SSN"]))
        assert len(binding.present) == 2
        assert [b.column.target for b in binding.landed] == ["pat_num"]
        assert [b.column.target for b in binding.prohibited] == ["ssn"]
        assert binding.new_columns == ()  # seen, so not reported as unknown drift

    def test_variant_landed_columns_exclude_prohibited(self):
        variant = SchemaVariant(
            name="v",
            detect=VariantDetector(require_all=["A"]),
            columns=[
                ColumnSpec(target="a", sources=["A"]),
                ColumnSpec(target="ssn", sources=["SSN"], pii=PiiClass.PROHIBITED),
            ],
        )
        assert [c.target for c in variant.landed_columns] == ["a"]


class TestRegistry:
    def test_filename_matching_returns_candidates_not_a_decision(self, cardinal):
        registry = SpecRegistry([cardinal])
        assert registry.candidates_for_filename("Cardinal_Invoice Review 2026.csv") == [cardinal]
        assert registry.candidates_for_filename("Cardinal_Invoice 01012023.xlsx") == [cardinal]
        assert registry.candidates_for_filename("ABC_Purchases.xlsx") == []

    def test_require_raises_helpfully(self, cardinal):
        with pytest.raises(KeyError, match="known:"):
            SpecRegistry([cardinal]).require("nope.nope")
