"""Tests for reading and loading a file.

Fixtures are synthetic but reproduce the exact quirks of the real extracts -
accounting negatives, two date formats, a trailing blank row, a superseded
variant, an SSN column. No PHI, so this runs in CI.
"""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path

import pytest

from sbh_data_bank.ingest.load import LoadOutcome, load_file, load_workbook
from sbh_data_bank.ingest.reader import open_source
from sbh_data_bank.registry.loader import SpecRegistry, load_spec
from sbh_data_bank.registry.spec import ReadSpec, SourceSpec
from sbh_data_bank.transforms.ndc import SetNdcDirectory

SPEC_DIR = Path(__file__).resolve().parents[2] / "specs"

CARDINAL_DETAIL_HEADER = (
    "Account #,Account Name,Account Alias,PO #,Invoice #,Invoice Line #,"
    "Invoice Date,Invoice Type,Purchase/Return,CIN,NDC,Trade Name,Generic Name,"
    "Unit price (Current Invoice),Original Ordered Qty,Ship Qty,"
    '"Purchase Dollars (Exclude Taxes, Fees & Surcharges)"'
)

CARDINAL_ROW = (
    "2150438824,PHARMA BUDIES LLC 340B,Rosemont Las Vegas Location,,7403554602,"
    "000010,2025-01-01,Invoice,Purchase,5738687,00310304000,SAPHNELO,"
    'ANIFROLUMAB-FNIA,"3,767.71",1,1,"$3,767.71 "'
)


@pytest.fixture(scope="module")
def registry() -> SpecRegistry:
    return SpecRegistry.from_dir(SPEC_DIR)


@pytest.fixture(scope="module")
def cardinal(registry) -> SourceSpec:
    return registry.require("wholesaler.cardinal.invoice")


def _write(tmp_path: Path, name: str, *lines: str) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestCardinalDetail:
    def test_a_clean_file_loads_and_balances(self, tmp_path, cardinal):
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW)
        load = load_file(path, cardinal)

        assert load.ok
        assert load.outcome == LoadOutcome.LOADED
        assert load.variant_name == "invoice_detail"
        assert load.counters.rows_read == 1
        assert load.counters.rows_ok == 1
        assert load.counters.balances

    def test_values_are_typed_not_strings(self, tmp_path, cardinal):
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW)
        row = load_file(path, cardinal).rows[0]

        assert row.values["purchase_amount"] == Decimal("3767.71")
        assert row.values["invoice_date"].isoformat() == "2025-01-01"
        assert row.values["ndc"] == "00310304000"
        assert row.values["invoice_line_number"] == "000010"  # leading zeros kept

    def test_the_trailing_blank_row_is_not_a_record(self, tmp_path, cardinal):
        """Cardinal detail exports end with a fully empty row."""
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW, "")
        load = load_file(path, cardinal)
        assert load.counters.rows_read == 1
        assert load.ok

    def test_an_interior_blank_row_is_quarantined_not_landed(self, tmp_path, cardinal):
        """A gap in the middle of a file is an anomaly, not formatting. Landing
        it would create a purchase with null everything."""
        path = _write(
            tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW, "", CARDINAL_ROW
        )
        load = load_file(path, cardinal)
        assert load.counters.rows_read == 3
        assert load.counters.rows_ok == 2
        assert load.counters.rows_quarantined == 1
        assert load.quarantined[0].reasons == ("BLANK_ROW",)
        assert load.counters.balances

    def test_a_missing_required_value_quarantines_the_row(self, tmp_path, cardinal):
        """`required` means the column exists AND the row has a value. A
        required field that silently accepts blanks is worse than no
        declaration at all."""
        blank_amount = CARDINAL_ROW.replace('"$3,767.71 "', "")
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, blank_amount)
        load = load_file(path, cardinal)
        assert load.counters.rows_quarantined == 1
        assert "MISSING_REQUIRED_VALUE" in load.quarantined[0].reasons

    def test_an_optional_column_may_be_blank(self, tmp_path, cardinal):
        blank_optional = CARDINAL_ROW.replace(",SAPHNELO,", ",,")
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, blank_optional)
        load = load_file(path, cardinal)
        assert load.counters.rows_ok == 1
        assert load.rows[0].values["trade_name"] is None

    def test_both_date_formats_load(self, tmp_path, cardinal):
        us_row = CARDINAL_ROW.replace("2025-01-01", "01/01/2025")
        path = _write(
            tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW, us_row
        )
        load = load_file(path, cardinal)
        assert load.counters.rows_ok == 2
        assert {r.values["invoice_date"].isoformat() for r in load.rows} == {"2025-01-01"}

    def test_a_control_total_is_produced_for_money(self, tmp_path, cardinal):
        path = _write(
            tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW, CARDINAL_ROW
        )
        load = load_file(path, cardinal)
        assert load.counters.control_totals["purchase_amount"] == Decimal("7535.42")

    def test_a_credit_reduces_the_control_total(self, tmp_path, cardinal):
        """Accounting negatives must reach the total, not vanish from it."""
        credit = CARDINAL_ROW.replace('"$3,767.71 "', '"($3,767.71)"')
        path = _write(
            tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW, credit
        )
        load = load_file(path, cardinal)
        assert load.counters.control_totals["purchase_amount"] == Decimal("0.00")

    def test_a_file_hash_is_recorded(self, tmp_path, cardinal):
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW)
        assert len(load_file(path, cardinal).file_hash) == 64

    def test_every_row_carries_a_hash(self, tmp_path, cardinal):
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW)
        assert len(load_file(path, cardinal).rows[0].values["_row_hash"]) == 64

    def test_identical_rows_hash_identically(self, tmp_path, cardinal):
        path = _write(
            tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW, CARDINAL_ROW
        )
        rows = load_file(path, cardinal).rows
        assert rows[0].values["_row_hash"] == rows[1].values["_row_hash"]


class TestOneBadRowNeverFailsAFile:
    def test_a_bad_date_quarantines_only_that_row(self, tmp_path, cardinal):
        bad = CARDINAL_ROW.replace("2025-01-01", "not-a-date")
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW, bad)
        load = load_file(path, cardinal)

        assert load.ok
        assert load.counters.rows_ok == 1
        assert load.counters.rows_quarantined == 1
        assert load.quarantined[0].reasons == ("BAD_DATE",)

    def test_the_quarantined_row_keeps_its_whole_payload(self, tmp_path, cardinal):
        """It has to be fixable and replayable without going back to the file."""
        bad = CARDINAL_ROW.replace("2025-01-01", "not-a-date")
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, bad)
        quarantined = load_file(path, cardinal).quarantined[0]

        assert quarantined.payload["invoice_number"] == "7403554602"
        assert quarantined.payload["invoice_date"] == "not-a-date"
        assert quarantined.errors[0].column == "invoice_date"

    def test_the_line_number_locates_the_row_in_the_original(self, tmp_path, cardinal):
        bad = CARDINAL_ROW.replace("2025-01-01", "not-a-date")
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW, bad)
        assert load_file(path, cardinal).quarantined[0].line_number == 3

    def test_an_unparseable_amount_quarantines_rather_than_nulling(self, tmp_path, cardinal):
        """The default is quarantine precisely so a money value cannot vanish."""
        bad = CARDINAL_ROW.replace('"$3,767.71 "', "not money")
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, bad)
        load = load_file(path, cardinal)
        assert load.counters.rows_quarantined == 1
        assert "NOT_NUMERIC" in load.quarantined[0].reasons

    def test_many_bad_rows_still_produce_a_balanced_load(self, tmp_path, cardinal):
        bad = CARDINAL_ROW.replace("2025-01-01", "nope")
        path = _write(
            tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, *([CARDINAL_ROW, bad] * 50)
        )
        load = load_file(path, cardinal)
        assert load.counters.rows_read == 100
        assert load.counters.rows_ok == 50
        assert load.counters.rows_quarantined == 50
        assert load.counters.balances
        assert load.quarantine_rate() == 0.5


class TestNdcResolution:
    def test_an_unhyphenated_ten_digit_ndc_needs_the_directory(self, tmp_path, cardinal):
        row = CARDINAL_ROW.replace("00310304000", "2568202801")
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, row)

        without = load_file(path, cardinal)
        assert without.counters.rows_quarantined == 1
        assert "NDC_NOT_FOUND" in without.quarantined[0].reasons

        with_directory = load_file(path, cardinal, ndc_directory=SetNdcDirectory({"25682002801"}))
        assert with_directory.counters.rows_ok == 1
        assert with_directory.rows[0].values["ndc"] == "25682002801"


class TestSupersededVariant:
    def test_the_cardinal_summary_is_recognised_and_not_loaded(self, tmp_path, cardinal):
        """Per the instruction to use the latest Cardinal file: the summary
        carries the same invoices as the detail report, so loading both would
        double-count every purchase."""
        header = (
            "Account #,Account Name,Account Alias,PO #,Invoice #,Invoice Date,"
            'Invoice Type,Purchase/Return,"Purchase Dollars (Exclude Taxes, Fees & Surcharges)"'
        )
        row = '2150438824,PHARMA BUDIES,alias,,7403554602,2025-01-01,Invoice,Purchase,"$3,767.71 "'
        path = _write(tmp_path, "Cardinal_Invoice old.csv", header, row)
        load = load_file(path, cardinal)

        assert load.outcome == LoadOutcome.RECOGNISED_NOT_LOADED
        assert load.variant_name == "invoice_summary"
        assert load.rows == []
        assert not load.ok
        assert "superseded by 'invoice_detail'" in load.detail


class TestUnrecognisedAndDrift:
    def test_an_unrecognised_file_loads_nothing_and_explains(self, tmp_path, cardinal):
        path = _write(tmp_path, "Cardinal_Invoice.csv", "Some,Other,Report", "1,2,3")
        load = load_file(path, cardinal)
        assert load.outcome == LoadOutcome.UNRECOGNISED
        assert load.rows == []
        assert "no variant matches" in load.detail

    def test_an_unknown_column_blocks_a_strict_source(self, tmp_path, registry):
        """finance.monthly_payments is on_new_column: fail."""
        spec = registry.require("finance.monthly_payments")
        header = ",".join(
            ["ClientID", "Voucher_Number", "Service_ID", "TransCode", "Amount", "Surprise"]
        )
        path = _write(tmp_path, "Monthly Payments 2025-06.csv", header, "1,2,3,X,$1.00,y")
        load = load_file(path, spec)
        assert load.outcome == LoadOutcome.BLOCKED_BY_DRIFT
        assert load.rows == []


class TestProhibitedColumns:
    def test_an_ssn_value_never_reaches_a_typed_row_or_quarantine(self, tmp_path, registry):
        """Including when a different column on the same row fails - which is
        the case that would otherwise leak an SSN into a quarantine table."""
        spec = registry.require("finance.monthly_payments")
        variant = spec.variants[0]
        header = ",".join(c.sources[0] for c in variant.columns)

        def row(amount: str) -> str:
            cells = []
            for column in variant.columns:
                if column.target == "ssn":
                    cells.append("123-45-6789")
                elif column.target == "amount":
                    cells.append(amount)
                elif column.type.value == "date":
                    cells.append("6/17/2025" if "%m/%d/%Y" in column.date_formats else "Jun 2025")
                elif column.type.value in {"integer", "decimal"}:
                    cells.append("1")
                elif column.type.value == "boolean":
                    cells.append("False")
                elif column.type.value == "currency":
                    cells.append("$1.00")
                else:
                    cells.append("x")
            return ",".join(cells)

        path = _write(tmp_path, "Monthly Payments 2025-06.csv", header, row("$1.00"), row("bad"))
        load = load_file(path, spec)

        assert load.counters.rows_ok == 1
        assert load.counters.rows_quarantined == 1

        assert "ssn" not in load.rows[0].values
        assert "123-45-6789" not in str(load.rows[0].values)

        quarantined = load.quarantined[0]
        assert "ssn" not in quarantined.payload
        assert "123-45-6789" not in str(quarantined.payload)
        assert "123-45-6789" not in str(quarantined.errors)


class TestStreams:
    def test_a_stream_loads_when_given_a_filename(self, cardinal):
        data = f"{CARDINAL_DETAIL_HEADER}\n{CARDINAL_ROW}\n".encode()
        load = load_file(io.BytesIO(data), cardinal, filename="from-a-zip.csv")
        assert load.ok
        assert load.counters.rows_ok == 1
        assert load.file_hash is None  # only computed for a real path

    def test_a_stream_without_a_filename_is_refused(self, cardinal):
        with pytest.raises(ValueError, match="filename is required"):
            load_file(io.BytesIO(b"a,b\n1,2\n"), cardinal)


class TestPreamble:
    def test_preamble_lines_are_skipped(self, tmp_path):
        """The ABC CSV opens with a sep= declaration and a report title."""
        path = _write(tmp_path, "x.csv", "sep=,", "Report title line", "A,B", "1,2")
        header, rows = open_source(path, ReadSpec(preamble_lines=2))
        assert header == ["A", "B"]
        assert [r.values for r in rows] == [("1", "2")]

    def test_line_numbers_account_for_the_preamble(self, tmp_path):
        path = _write(tmp_path, "x.csv", "sep=,", "title", "A,B", "1,2")
        _, rows = open_source(path, ReadSpec(preamble_lines=2))
        assert next(iter(rows)).line_number == 4


class TestWorkbook:
    def test_each_selected_sheet_is_its_own_load(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        spec = load_spec(SPEC_DIR / "reference.wholesaler_accounts.yaml")

        workbook = openpyxl.Workbook()
        master = workbook.active
        master.title = "Master Sheet"
        master.append(
            ["Payee", "Class", "Class_New", "Changed?", "Category", "Account No", "Location"]
        )
        master.append(
            [
                "CARDINAL HEALTH",
                "Fire Mesa ID",
                "Fire Mesa ID",
                "No",
                "Pharmacy Expense",
                "2057232983",
                "SAGEBRUSH FM",
            ]
        )

        tab = workbook.create_sheet("Cardinal")
        tab.append(["Payee", "Class", "Category", "Account No", "Location"])
        tab.append(
            ["CARDINAL HEALTH", "Fire Mesa ID", "Pharmacy Expense", "2057232983", "SAGEBRUSH FM"]
        )

        path = tmp_path / "Master COGS List.xlsx"
        workbook.save(path)

        loads = load_workbook(path, spec)
        by_sheet = {load.sheet: load for load in loads}

        assert by_sheet["Master Sheet"].variant_name == "master_sheet"
        assert by_sheet["Cardinal"].variant_name == "vendor_tab"
        assert all(load.ok for load in loads)

    def test_the_origin_sheet_is_recorded_on_every_row(self, tmp_path):
        """Once tabs are concatenated their origin is otherwise unrecoverable,
        and it is what separates the authoritative sheet from a working copy."""
        openpyxl = pytest.importorskip("openpyxl")
        spec = load_spec(SPEC_DIR / "reference.wholesaler_accounts.yaml")

        workbook = openpyxl.Workbook()
        tab = workbook.active
        tab.title = "Cardinal"
        tab.append(["Payee", "Class", "Category", "Account No", "Location"])
        tab.append(
            ["CARDINAL HEALTH", "Fire Mesa ID", "Pharmacy Expense", "2057232983", "SAGEBRUSH FM"]
        )
        path = tmp_path / "Master COGS List.xlsx"
        workbook.save(path)

        load = load_workbook(path, spec)[0]
        assert load.rows[0].values["source_sheet"] == "Cardinal"

    def test_all_sheets_of_one_workbook_share_a_file_hash(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        spec = load_spec(SPEC_DIR / "reference.wholesaler_accounts.yaml")
        workbook = openpyxl.Workbook()
        tab = workbook.active
        tab.title = "Cardinal"
        tab.append(["Payee", "Class", "Category", "Account No", "Location"])
        tab.append(["CARDINAL HEALTH", "x", "y", "1", "z"])
        second = workbook.create_sheet("ABC")
        second.append(["Payee", "Class", "Category", "Account No", "Location"])
        second.append(["ABC", "x", "y", "2", "z"])
        path = tmp_path / "Master COGS List.xlsx"
        workbook.save(path)

        loads = load_workbook(path, spec)
        assert len({load.file_hash for load in loads}) == 1


class TestTotalsFooter:
    """The Cardinal detail export ends with a totals line, and it is worth more
    than an annoyance to skip: the vendor did that arithmetic independently, so
    agreement is evidence every row was read and every value parsed."""

    def _file(self, tmp_path, *rows, footer: str) -> Path:
        return _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, *rows, footer)

    def test_the_footer_is_not_landed_as_a_purchase(self, tmp_path, cardinal):
        """Landing it would have added $20.1M of phantom purchases to a $20.1M
        file, doubling it exactly."""
        footer = "," * 14 + '1,1,"$3,767.71 "'
        path = self._file(tmp_path, CARDINAL_ROW, footer=footer)
        load = load_file(path, cardinal)

        assert load.counters.rows_read == 1
        assert load.counters.rows_ok == 1
        assert load.counters.control_totals["purchase_amount"] == Decimal("3767.71")

    def test_declared_totals_are_checked_against_computed(self, tmp_path, cardinal):
        footer = "," * 14 + '2,2,"$7,535.42"'
        path = self._file(tmp_path, CARDINAL_ROW, CARDINAL_ROW, footer=footer)
        load = load_file(path, cardinal)

        assert load.ok
        assert load.footer is not None
        assert load.footer.agrees
        assert load.footer.declared["purchase_amount"] == Decimal("7535.42")
        assert "file totals agree" in load.footer.describe()

    def test_a_disagreement_blocks_the_load(self, tmp_path, cardinal):
        """If the file says one thing and we computed another, rows were lost
        or values misparsed. Either way nothing commits."""
        footer = "," * 14 + '2,2,"$9,999.99"'
        path = self._file(tmp_path, CARDINAL_ROW, CARDINAL_ROW, footer=footer)
        load = load_file(path, cardinal)

        assert not load.ok
        assert load.outcome == LoadOutcome.CONTROL_TOTAL_MISMATCH
        assert not load.footer.agrees
        assert "declared 9,999.99" in load.footer.describe()
        assert "computed 7,535.42" in load.footer.describe()

    def test_a_footer_would_catch_dropped_credits(self, tmp_path, cardinal):
        """The check that would have caught the $39.97M payments defect: if
        parenthesised credits were nulled, the computed total would exceed the
        declared one."""
        credit = CARDINAL_ROW.replace('"$3,767.71 "', '"($3,767.71)"')
        footer = "," * 14 + '2,2,"$0.00"'
        path = self._file(tmp_path, CARDINAL_ROW, credit, footer=footer)
        load = load_file(path, cardinal)

        assert load.ok
        assert load.footer.agrees
        assert load.counters.control_totals["purchase_amount"] == Decimal("0.00")

    def test_a_data_row_missing_its_key_still_quarantines(self, tmp_path, cardinal):
        """Footer detection must not become an escape hatch for broken rows.

        This row has content, so it is not a blank line, but no natural key and
        no totals - a broken data row, not a footer, and it quarantines.
        """
        broken = ("," * 11) + "SAPHNELO" + ("," * 5)
        path = _write(
            tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW, broken
        )
        load = load_file(path, cardinal)

        assert load.footer is None
        assert load.counters.rows_ok == 1
        assert load.counters.rows_quarantined == 1
        assert "MISSING_REQUIRED_VALUE" in load.quarantined[0].reasons

    def test_only_the_last_row_can_be_a_footer(self, tmp_path, cardinal):
        """A totals-shaped row in the middle of a file is an anomaly."""
        footer_shaped = "," * 14 + '1,1,"$3,767.71 "'
        path = _write(
            tmp_path,
            "Cardinal_Invoice.csv",
            CARDINAL_DETAIL_HEADER,
            footer_shaped,
            CARDINAL_ROW,
        )
        load = load_file(path, cardinal)
        assert load.footer is None
        assert load.counters.rows_quarantined == 1

    def test_a_source_without_a_footer_policy_quarantines_a_totals_row(self, tmp_path, registry):
        """finance.monthly_payments declares no footer, so a totals-shaped row
        is the anomaly it would be there, not something to skip."""
        spec = registry.require("finance.monthly_payments")
        variant = spec.variants[0]
        header = ",".join(c.sources[0] for c in variant.columns)
        amount_index = next(i for i, c in enumerate(variant.columns) if c.target == "amount")
        cells = [""] * len(variant.columns)
        cells[amount_index] = "$1,000.00"
        path = _write(tmp_path, "Monthly Payments 2025-06.csv", header, ",".join(cells))
        load = load_file(path, spec)

        assert load.footer is None
        assert load.counters.rows_read == 1
        assert load.counters.rows_quarantined == 1
