"""Tests for spec loading, variant selection and column binding.

Covers three modules in ``sbh_data_bank.registry``:

- ``spec.py``     the pydantic model a YAML spec is validated into
- ``loader.py``   reading spec files and the registry of all of them
- ``binding.py``  matching one file's header row to exactly one variant of a
                  spec, then every spec column to a header position

"Binding" is the step between "here is a file" and "read its rows": it decides
which variant the file is, where each target column lives, which required
columns are missing, and which vendor columns the spec does not know about.

The Cardinal spec is exercised against the exact header rows of all four
Cardinal files in the estate, so the suite fails if a real file stops binding.

How to read this file:

- TestSpecLoading       YAML to SourceSpec; a typo is an error, not a default
- TestSpecValidation    the model's own rules (dates need formats, keys exist...)
- TestVariantSelection  exactly one variant per file, or a diagnosable error
- TestColumnBinding     every column of the real Cardinal header binds
- TestDrift             new, missing and renamed columns and the policy applied
- TestProhibitedColumns a prohibited (SSN) column is seen but never landed
- TestRegistry          filename matching returns candidates, not a decision
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
    """The shipped Cardinal spec, loaded once for the module."""
    return load_spec(SPEC_DIR / "wholesaler.cardinal.invoice.yaml")


class TestSpecLoading:
    """``load_spec`` and ``SpecRegistry``: YAML in, validated model out."""

    def test_the_cardinal_spec_loads(self, cardinal):
        """The real spec parses and declares its two variants in order."""
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
    """Rules the ``SourceSpec`` model enforces at load time, so a bad spec
    fails in CI rather than at 2am against a real file."""

    def _minimal(self, **overrides):
        """The smallest valid spec; each test breaks one thing about it."""
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
        """The rule "never infer a date format" is enforced by the model, not
        by convention."""
        with pytest.raises(ValueError, match="must declare date_formats"):
            ColumnSpec(target="d", sources=["D"], type=FieldType.DATE)

    def test_a_non_date_column_cannot_declare_formats(self):
        """Formats on a text column would be silently ignored, which usually
        means the type is wrong."""
        with pytest.raises(ValueError, match="cannot have date_formats"):
            ColumnSpec(target="t", sources=["T"], type=FieldType.TEXT, date_formats=["%Y"])

    def test_a_detector_must_assert_something(self):
        """An empty detector would match every file."""
        with pytest.raises(ValueError, match="at least one column"):
            VariantDetector()

    def test_a_variant_cannot_map_the_same_target_twice(self):
        """Two source columns landing in one target: the second would
        silently overwrite the first."""
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

    def test_a_vendor_column_cannot_take_a_ledger_column_name(self):
        """Every generated table carries ``created_at`` and the other ledger
        columns; the first Azure migration failed on WeInfuse's ``Created
        At`` mapped to ``created_at`` (2026-10-21)."""
        spec = self._minimal()
        spec["variants"][0]["columns"].append({"target": "created_at", "sources": ["Created At"]})
        with pytest.raises(ValueError, match="ledger column names"):
            SourceSpec.model_validate(spec)

    def test_a_constant_cannot_overwrite_a_mapped_column(self):
        """``constants`` injects fixed values (Pharmaforce uses it for the
        site); it must not shadow a column the file actually carries."""
        with pytest.raises(ValueError, match="constants would overwrite"):
            SourceSpec.model_validate(self._minimal(constants={"a": "x"}))

    def test_period_pattern_needs_year_and_month_groups(self):
        """``period_from_filename`` must capture (?P<year>) and (?P<month>),
        or the period it recovers cannot be used."""
        with pytest.raises(ValueError, match="named groups"):
            SourceSpec.model_validate(self._minimal(period_from_filename=r"(\d{4})"))

    def test_variant_names_must_be_unique(self):
        """Variant names appear in batch records; two with one name would be
        indistinguishable afterwards."""
        spec = self._minimal()
        spec["variants"] = spec["variants"] * 2
        with pytest.raises(ValueError, match="variant names must be unique"):
            SourceSpec.model_validate(spec)


class TestVariantSelection:
    """``select_variant``: exactly one variant's detector must match the
    file's headers. Zero and more than one are both errors."""

    def test_the_detail_report_selects_the_detail_variant(self, cardinal):
        """The 17-column header carries NDC, Invoice Line # and Ship Qty."""
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
        """The error says what each variant wanted and what the file had, so
        the operator can tell a wrong file from a new vendor layout."""
        with pytest.raises(NoVariantMatchedError) as excinfo:
            select_variant(cardinal, HeaderIndex(["Some", "Other", "Report"]))
        message = str(excinfo.value)
        assert "invoice_detail" in message and "invoice_summary" in message
        assert "missing" in message
        assert "file has 3 columns" in message

    def test_overlapping_detectors_are_a_spec_bug(self):
        """Two variants both satisfied by one file is a mistake in the spec,
        and the error says how to fix it (add require_none)."""
        # Two variants with identical detectors: both match any file with "A".
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
    """``bind``: after the variant is chosen, every spec column is matched to
    a header position (or recorded as missing)."""

    def test_every_detail_column_binds(self, cardinal):
        """All 17 columns of the real detail header bind, nothing is missing
        and nothing is unclaimed."""
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
        """A binding carries the column position the reader will use and the
        type the parser will apply."""
        binding = bind(cardinal, HeaderIndex(CARDINAL_DETAIL))
        ndc = next(b for b in binding.columns if b.column.target == "ndc")
        assert ndc.match.position == 10
        assert ndc.column.type is FieldType.NDC

    def test_the_punctuation_heavy_amount_column_binds(self, cardinal):
        """The longest, most punctuated Cardinal header binds to the currency
        column that carries the control total."""
        binding = bind(cardinal, HeaderIndex(CARDINAL_DETAIL))
        amount = next(b for b in binding.columns if b.column.target == "purchase_amount")
        assert amount.match.source_header == "Purchase Dollars (Exclude Taxes, Fees & Surcharges)"
        assert amount.column.type is FieldType.CURRENCY


class TestDrift:
    """Schema drift: a column the vendor added, removed or renamed. The spec's
    ``on_new_column`` and ``on_missing_column`` policies decide whether the
    load proceeds; nothing is ever silently dropped."""

    def test_a_new_vendor_column_is_reported_not_dropped(self, cardinal):
        """An unknown column shows up in ``new_columns`` and sets has_drift."""
        binding = bind(cardinal, HeaderIndex([*CARDINAL_DETAIL, "Rebate Amount"]))
        assert binding.new_columns == ("Rebate Amount",)
        assert binding.has_drift

    def test_a_new_column_only_warns_for_this_source(self, cardinal):
        """Cardinal's policy is on_new_column: warn, so the file still loads."""
        binding = bind(cardinal, HeaderIndex([*CARDINAL_DETAIL, "Rebate Amount"]))
        assert cardinal.on_new_column is DriftPolicy.WARN
        assert binding.blocking_problem() is None

    def test_a_missing_required_column_blocks_the_load(self, cardinal):
        """Dropping Ship Qty, which is both required and a detector marker."""
        headers = [h for h in CARDINAL_DETAIL if h != "Ship Qty"]
        # Removing Ship Qty also breaks the detail detector, so the file is
        # simply unrecognisable - which is the correct, louder outcome.
        with pytest.raises(NoVariantMatchedError):
            bind(cardinal, HeaderIndex(headers))

    def test_a_required_column_absent_but_still_detectable_blocks(self, cardinal):
        """Dropping Purchase/Return: not a detector marker, so the variant is
        still recognised, but it is required and the policy is fail."""
        headers = [h for h in CARDINAL_DETAIL if h != "Purchase/Return"]
        binding = bind(cardinal, HeaderIndex(headers))
        assert "purchase_or_return" in binding.missing_required
        assert cardinal.on_missing_column is DriftPolicy.FAIL
        assert binding.blocking_problem() is not None

    def test_a_deprecated_alias_is_surfaced(self):
        """When a column binds via a later alias (the old name), the binding
        says so, because the vendor may be about to drop that name."""
        # The spec lists the current name first and the legacy name second;
        # the file carries only the legacy name.
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
        """``drift_report`` is the text that goes in the batch record and the
        alert; it names the column."""
        binding = bind(cardinal, HeaderIndex([*CARDINAL_DETAIL, "Rebate Amount"]))
        assert "Rebate Amount" in binding.drift_report()


class TestProhibitedColumns:
    """``pii: prohibited`` (SSN in Monthly Payments and Curve Monthly
    Services): the column is bound so drift checks see it, and excluded from
    everything that persists."""

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
        # Both columns are present (matched)...
        assert len(binding.present) == 2
        # ...but only one is landed; SSN is on the prohibited list instead.
        assert [b.column.target for b in binding.landed] == ["pat_num"]
        assert [b.column.target for b in binding.prohibited] == ["ssn"]
        assert binding.new_columns == ()  # seen, so not reported as unknown drift

    def test_variant_landed_columns_exclude_prohibited(self):
        """The same rule at the model level: ``landed_columns`` is what the
        schema generator and the row writer iterate."""
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
    """``SpecRegistry``: the collection of loaded specs."""

    def test_filename_matching_returns_candidates_not_a_decision(self, cardinal):
        """``filename_patterns`` narrows which specs to try; the header
        fingerprint makes the actual decision. Both Cardinal filenames match
        the one spec, and its two variants sort them out from there."""
        registry = SpecRegistry([cardinal])
        assert registry.candidates_for_filename("Cardinal_Invoice Review 2026.csv") == [cardinal]
        assert registry.candidates_for_filename("Cardinal_Invoice 01012023.xlsx") == [cardinal]
        assert registry.candidates_for_filename("ABC_Purchases.xlsx") == []

    def test_require_raises_helpfully(self, cardinal):
        """An unknown key lists the known ones."""
        with pytest.raises(KeyError, match="known:"):
            SpecRegistry([cardinal]).require("nope.nope")
