"""Tests for reading and loading a file - ``sbh_data_bank.ingest``.

``load_file`` is the whole pipeline from a path to typed rows: open the file
(``reader.py``), find the header, bind it to a spec variant, parse every cell,
quarantine rows that fail, keep running control totals, and check them against
the vendor's own footer if the spec declares one. Nothing here writes to a
database; the result is a ``FileLoad`` object the commit engine will consume.

Fixtures are synthetic but reproduce the exact quirks of the real extracts -
accounting negatives, two date formats, a trailing blank row, a superseded
variant, an SSN column. No PHI, so this runs in CI.

Two module-level constants carry the real 17-column Cardinal detail header and
one realistic row; most tests build a small CSV from them with ``_write`` and
then mutate one cell to provoke a specific behaviour.

How to read this file:

- TestCardinalDetail          the happy path and row accounting
- TestOneBadRowNeverFailsAFile  quarantine isolates a bad row; the file still loads
- TestNdcResolution           10-digit NDCs need the directory
- TestSupersededVariant       the Cardinal summary is recognised and skipped
- TestUnrecognisedAndDrift    outcomes for an unknown file and a strict spec
- TestProhibitedColumns       an SSN value never persists, even on a failing row
- TestStreams                 loading from a byte stream (a zip member)
- TestPreamble                skipping title lines by count
- TestWorkbook                one load per worksheet, sheet name recorded
- TestTotalsFooter            the vendor's totals row as a control total
- TestHeaderMarker            finding the header by a column name instead
"""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path

import pytest

from sbh_data_bank.ingest.load import LoadOutcome, load_file, load_workbook
from sbh_data_bank.ingest.reader import HeaderNotFoundError, open_source
from sbh_data_bank.registry.loader import SpecRegistry, load_spec
from sbh_data_bank.registry.spec import ReadSpec, SourceSpec
from sbh_data_bank.transforms.ndc import SetNdcDirectory

SPEC_DIR = Path(__file__).resolve().parents[2] / "specs"

# The real 17-column Cardinal detail header, as one CSV line. The last header
# contains a comma, so it is quoted.
CARDINAL_DETAIL_HEADER = (
    "Account #,Account Name,Account Alias,PO #,Invoice #,Invoice Line #,"
    "Invoice Date,Invoice Type,Purchase/Return,CIN,NDC,Trade Name,Generic Name,"
    "Unit price (Current Invoice),Original Ordered Qty,Ship Qty,"
    '"Purchase Dollars (Exclude Taxes, Fees & Surcharges)"'
)

# One realistic row in the same column order: account, name, alias, PO,
# invoice, line "000010" (leading zeros matter), ISO date, type,
# Purchase/Return, CIN, 11-digit NDC, trade and generic names, unit price with
# a quoted thousands separator, ordered qty, shipped qty, and the amount with
# a dollar sign and a trailing space - exactly as Cardinal ships it.
CARDINAL_ROW = (
    "2150438824,PHARMA BUDIES LLC 340B,Rosemont Las Vegas Location,,7403554602,"
    "000010,2025-01-01,Invoice,Purchase,5738687,00310304000,SAPHNELO,"
    'ANIFROLUMAB-FNIA,"3,767.71",1,1,"$3,767.71 "'
)


@pytest.fixture(scope="module")
def registry() -> SpecRegistry:
    """Every shipped spec, loaded once."""
    return SpecRegistry.from_dir(SPEC_DIR)


@pytest.fixture(scope="module")
def cardinal(registry) -> SourceSpec:
    """The Cardinal spec most tests exercise."""
    return registry.require("wholesaler.cardinal.invoice")


def _write(tmp_path: Path, name: str, *lines: str) -> Path:
    """Write a small CSV: one line per argument, newline-terminated."""
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestCardinalDetail:
    """The happy path through ``load_file`` and the row accounting around it."""

    def test_a_clean_file_loads_and_balances(self, tmp_path, cardinal):
        """One good row: loaded, the detail variant chosen, and the counters
        balance (rows read == rows ok + rows quarantined)."""
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW)
        load = load_file(path, cardinal)

        assert load.ok
        assert load.outcome == LoadOutcome.LOADED
        assert load.variant_name == "invoice_detail"
        assert load.counters.rows_read == 1
        assert load.counters.rows_ok == 1
        assert load.counters.balances

    def test_values_are_typed_not_strings(self, tmp_path, cardinal):
        """From the definition of done, "every destination column is typed":
        money is Decimal, the date is a date, the NDC is 11 digits, and a
        zero-padded identifier stays text."""
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
        """Trade name is not required; a blank lands as null, not quarantine."""
        blank_optional = CARDINAL_ROW.replace(",SAPHNELO,", ",,")
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, blank_optional)
        load = load_file(path, cardinal)
        assert load.counters.rows_ok == 1
        assert load.rows[0].values["trade_name"] is None

    def test_both_date_formats_load(self, tmp_path, cardinal):
        """Cardinal ships ISO in one pull and US in the next (finding 3). The
        spec declares both, so one file with both forms parses to one date."""
        us_row = CARDINAL_ROW.replace("2025-01-01", "01/01/2025")
        path = _write(
            tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW, us_row
        )
        load = load_file(path, cardinal)
        assert load.counters.rows_ok == 2
        assert {r.values["invoice_date"].isoformat() for r in load.rows} == {"2025-01-01"}

    def test_a_control_total_is_produced_for_money(self, tmp_path, cardinal):
        """Columns marked ``control_total: true`` are summed as rows load;
        two rows of $3,767.71 give $7,535.42 exactly."""
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
        """The SHA-256 that identifies this file in ``meta.source_file``."""
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW)
        assert len(load_file(path, cardinal).file_hash) == 64

    def test_every_row_carries_a_hash(self, tmp_path, cardinal):
        """``_row_hash`` is the row identity used for dedup and supersession."""
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW)
        assert len(load_file(path, cardinal).rows[0].values["_row_hash"]) == 64

    def test_identical_rows_hash_identically(self, tmp_path, cardinal):
        """Two byte-identical rows get one hash; the dedup policy, not the
        loader, decides what to do about that."""
        path = _write(
            tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, CARDINAL_ROW, CARDINAL_ROW
        )
        rows = load_file(path, cardinal).rows
        assert rows[0].values["_row_hash"] == rows[1].values["_row_hash"]


class TestOneBadRowNeverFailsAFile:
    """From the definition of done, "one bad row never fails a file". A row
    that fails parsing goes to quarantine with its reason and full payload;
    the rest of the file loads normally."""

    def test_a_bad_date_quarantines_only_that_row(self, tmp_path, cardinal):
        """One good row and one with an unparseable date: one ok, one
        quarantined with reason BAD_DATE, and the load is still ok."""
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
        """Line 1 is the header, line 2 the good row, so the bad row is 3.
        An operator can open the file and go straight to it."""
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
        """Fifty good and fifty bad rows interleaved: every row is accounted
        for and the quarantine rate is reported, so a threshold abort can be
        layered on top without re-reading anything."""
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
    """The ``ndc`` column type runs ``normalize_ndc``; an optional NDC
    directory passed to ``load_file`` is what resolves 10-digit values."""

    def test_an_unhyphenated_ten_digit_ndc_needs_the_directory(self, tmp_path, cardinal):
        """Same file twice: without a directory the row quarantines as
        NDC_NOT_FOUND; with one that knows the 5-3-2 expansion it loads."""
        row = CARDINAL_ROW.replace("00310304000", "2568202801")
        path = _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, row)

        without = load_file(path, cardinal)
        assert without.counters.rows_quarantined == 1
        assert "NDC_NOT_FOUND" in without.quarantined[0].reasons

        with_directory = load_file(path, cardinal, ndc_directory=SetNdcDirectory({"25682002801"}))
        assert with_directory.counters.rows_ok == 1
        assert with_directory.rows[0].values["ndc"] == "25682002801"


class TestSupersededVariant:
    """A variant with ``action: recognise_only``: the file is identified, so
    it does not raise an unknown-file alert, but nothing is loaded."""

    def test_the_cardinal_summary_is_recognised_and_not_loaded(self, tmp_path, cardinal):
        """Per the instruction to use the latest Cardinal file: the summary
        carries the same invoices as the detail report, so loading both would
        double-count every purchase."""
        # The 9-column summary header: no NDC, no line number, no quantities.
        header = (
            "Account #,Account Name,Account Alias,PO #,Invoice #,Invoice Date,"
            'Invoice Type,Purchase/Return,"Purchase Dollars (Exclude Taxes, Fees & Surcharges)"'
        )
        row = '2150438824,PHARMA BUDIES,alias,,7403554602,2025-01-01,Invoice,Purchase,"$3,767.71 "'
        path = _write(tmp_path, "Cardinal_Invoice old.csv", header, row)
        load = load_file(path, cardinal)

        # Identified as the summary variant, zero rows, not ok (so nothing
        # commits), and the detail explains which variant replaces it.
        assert load.outcome == LoadOutcome.RECOGNISED_NOT_LOADED
        assert load.variant_name == "invoice_summary"
        assert load.rows == []
        assert not load.ok
        assert "superseded by 'invoice_detail'" in load.detail


class TestUnrecognisedAndDrift:
    """The two outcomes where a file is refused before any row is read."""

    def test_an_unrecognised_file_loads_nothing_and_explains(self, tmp_path, cardinal):
        """A header matching no variant: outcome UNRECOGNISED, zero rows, and
        the variant-selection diagnosis carried in ``detail``."""
        path = _write(tmp_path, "Cardinal_Invoice.csv", "Some,Other,Report", "1,2,3")
        load = load_file(path, cardinal)
        assert load.outcome == LoadOutcome.UNRECOGNISED
        assert load.rows == []
        assert "no variant matches" in load.detail

    def test_an_unknown_column_blocks_a_strict_source(self, tmp_path, registry):
        """finance.monthly_payments is on_new_column: fail."""
        spec = registry.require("finance.monthly_payments")
        # Enough real columns to satisfy the detector, plus one the spec has
        # never seen.
        header = ",".join(
            ["ClientID", "Voucher_Number", "Service_ID", "TransCode", "Amount", "Surprise"]
        )
        path = _write(tmp_path, "Monthly Payments 2025-06.csv", header, "1,2,3,X,$1.00,y")
        load = load_file(path, spec)
        assert load.outcome == LoadOutcome.BLOCKED_BY_DRIFT
        assert load.rows == []


class TestProhibitedColumns:
    """SSN is 69.1% populated in Monthly Payments (finding 7) and is never
    landed. This test proves the value does not survive in any output of a
    load, including the quarantine payload of a row that failed elsewhere."""

    def test_an_ssn_value_never_reaches_a_typed_row_or_quarantine(self, tmp_path, registry):
        """Including when a different column on the same row fails - which is
        the case that would otherwise leak an SSN into a quarantine table."""
        spec = registry.require("finance.monthly_payments")
        variant = spec.variants[0]
        # Build the full 116-column header from the spec itself, so the test
        # follows the spec if columns are ever added.
        header = ",".join(c.sources[0] for c in variant.columns)

        def row(amount: str) -> str:
            # One cell per column, chosen by type so every parser succeeds;
            # the SSN cell carries a recognisable fake, and ``amount`` is the
            # one cell the caller controls so it can be made to fail.
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

        # One row that loads and one whose amount fails to parse.
        path = _write(tmp_path, "Monthly Payments 2025-06.csv", header, row("$1.00"), row("bad"))
        load = load_file(path, spec)

        assert load.counters.rows_ok == 1
        assert load.counters.rows_quarantined == 1

        # The landed row: no ssn key, and the value nowhere in it.
        assert "ssn" not in load.rows[0].values
        assert "123-45-6789" not in str(load.rows[0].values)

        # The quarantined row: same, in the payload and in the error text.
        quarantined = load.quarantined[0]
        assert "ssn" not in quarantined.payload
        assert "123-45-6789" not in str(quarantined.payload)
        assert "123-45-6789" not in str(quarantined.errors)


class TestStreams:
    """``load_file`` also accepts a binary stream, which is how the members of
    ``Monthly Payments.zip`` are read without extracting them to disk."""

    def test_a_stream_loads_when_given_a_filename(self, cardinal):
        """A stream has no path, so the caller supplies the name for the
        record; the file hash is only computed for a real path."""
        data = f"{CARDINAL_DETAIL_HEADER}\n{CARDINAL_ROW}\n".encode()
        load = load_file(io.BytesIO(data), cardinal, filename="from-a-zip.csv")
        assert load.ok
        assert load.counters.rows_ok == 1
        assert load.file_hash is None  # only computed for a real path

    def test_a_stream_without_a_filename_is_refused(self, cardinal):
        """A load with no file name could not be traced back; refuse it."""
        with pytest.raises(ValueError, match="filename is required"):
            load_file(io.BytesIO(b"a,b\n1,2\n"), cardinal)


class TestPreamble:
    """``preamble_lines``: skip a fixed number of lines before the header.
    This is the simple mechanism; TestHeaderMarker covers the other."""

    def test_preamble_lines_are_skipped(self, tmp_path):
        """The ABC CSV opens with a sep= declaration and a report title."""
        path = _write(tmp_path, "x.csv", "sep=,", "Report title line", "A,B", "1,2")
        header, rows = open_source(path, ReadSpec(preamble_lines=2))
        assert header == ["A", "B"]
        assert [r.values for r in rows] == [("1", "2")]

    def test_line_numbers_account_for_the_preamble(self, tmp_path):
        """Line numbers are physical file lines (1-based), so a quarantined
        row can be found in the original: two preamble lines, header, data = 4."""
        path = _write(tmp_path, "x.csv", "sep=,", "title", "A,B", "1,2")
        _, rows = open_source(path, ReadSpec(preamble_lines=2))
        assert next(iter(rows)).line_number == 4


class TestWorkbook:
    """``load_workbook``: an Excel file is one load per selected worksheet.
    The Master COGS List spec is used because it has two variants - the
    consolidated Master Sheet and the per-vendor tabs - in one workbook."""

    def test_each_selected_sheet_is_its_own_load(self, tmp_path):
        """Two sheets with different headers bind to different variants, and
        both load."""
        openpyxl = pytest.importorskip("openpyxl")
        spec = load_spec(SPEC_DIR / "reference.wholesaler_accounts.yaml")

        # Master Sheet has the extra Class_New and Changed? columns that
        # distinguish it from a vendor tab.
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

        # A vendor tab: the five-column layout.
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
        """One physical file, one hash: ``meta.source_file`` is keyed on
        (hash, sheet), so each sheet is a distinct row under the same hash."""
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
        """A Cardinal CSV with the given rows and a final footer line."""
        return _write(tmp_path, "Cardinal_Invoice.csv", CARDINAL_DETAIL_HEADER, *rows, footer)

    # A footer line is 14 empty cells (the key and descriptive columns) and
    # then the three control-total columns: ordered qty, ship qty, amount.
    # That shape - key columns blank, totals populated, last row - is what
    # ``footer: totals_row`` recognises.

    def test_the_footer_is_not_landed_as_a_purchase(self, tmp_path, cardinal):
        """Landing it would have added $20.1M of phantom purchases to a $20.1M
        file, doubling it exactly."""
        footer = "," * 14 + '1,1,"$3,767.71 "'
        path = self._file(tmp_path, CARDINAL_ROW, footer=footer)
        load = load_file(path, cardinal)

        # One data row read, the footer not counted as a row at all, and the
        # control total is the data row alone.
        assert load.counters.rows_read == 1
        assert load.counters.rows_ok == 1
        assert load.counters.control_totals["purchase_amount"] == Decimal("3767.71")

    def test_declared_totals_are_checked_against_computed(self, tmp_path, cardinal):
        """The footer's figures are parsed as the vendor's declared totals and
        compared with ours; agreement is recorded on the load."""
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
        # Only the trade-name cell (position 11) is filled; no key, no totals.
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
        # A row that is blank except for the amount: footer-shaped.
        amount_index = next(i for i, c in enumerate(variant.columns) if c.target == "amount")
        cells = [""] * len(variant.columns)
        cells[amount_index] = "$1,000.00"
        path = _write(tmp_path, "Monthly Payments 2025-06.csv", header, ",".join(cells))
        load = load_file(path, spec)

        # No footer policy, so it is read as a data row and quarantined for
        # its missing required values.
        assert load.footer is None
        assert load.counters.rows_read == 1
        assert load.counters.rows_quarantined == 1


class TestHeaderMarker:
    """ABC delivers the same 26 columns with one title row above the header as a
    workbook, and two lines - a `sep=` declaration plus a title - as a CSV. A
    fixed preamble count cannot serve both, and two variants cannot be told
    apart because their headers are identical. So the header is located by a
    column name instead of by counting junk lines."""

    # A two-column stand-in for the ABC spec. The only thing that matters is
    # ``read.header_marker``: the reader scans down the file for the row that
    # carries this column name and treats it as the header.
    SPEC = {
        "key": "wholesaler.abc.purchases",
        "name": "ABC",
        "vendor": "AmerisourceBergen",
        "natural_key": ["account_number", "ndc"],
        "variants": [
            {
                "name": "invoice_detail",
                "detect": {"require_all": ["Account #", "NDC"]},
                "read": {"header_marker": "Account #"},
                "columns": [
                    {"target": "account_number", "sources": ["Account #"]},
                    {"target": "ndc", "sources": ["NDC"], "type": "ndc"},
                ],
            }
        ],
    }

    def _spec(self) -> SourceSpec:
        return SourceSpec.model_validate(self.SPEC)

    def test_finds_the_header_under_one_preamble_line(self, tmp_path):
        """The workbook form: one title row, then the header."""
        path = _write(
            tmp_path,
            "ABC_Purchases.csv",
            "Purchases-Invoice Detail-Run Date-03/07/2026",
            "Account #,NDC",
            "0100461951,00310304000",
        )
        load = load_file(path, self._spec())
        assert load.ok
        assert load.rows[0].values["account_number"] == "0100461951"

    def test_finds_the_header_under_two_preamble_lines(self, tmp_path):
        """The same spec, unchanged, on the CSV form of the same report."""
        path = _write(
            tmp_path,
            "ABC_Purchases.csv",
            "sep=,",
            "Purchases-Invoice Detail-Run Date-02/05/2026",
            "Account #,NDC",
            "0100461951,00310304000",
        )
        load = load_file(path, self._spec())
        assert load.ok
        assert load.counters.rows_ok == 1

    def test_finds_the_header_with_no_preamble_at_all(self, tmp_path):
        """A marker on line 1 is fine too; the scan starts at the top."""
        path = _write(tmp_path, "ABC_Purchases.csv", "Account #,NDC", "0100461951,00310304000")
        assert load_file(path, self._spec()).ok

    def test_leading_zeros_survive_because_the_account_is_text(self, tmp_path):
        """ABC accounts are 0100583154 in the extracts and 100583154 in the
        Master COGS List. An integer conversion would erase the distinction."""
        path = _write(tmp_path, "ABC_Purchases.csv", "Account #,NDC", "0100583154,00310304000")
        assert load_file(path, self._spec()).rows[0].values["account_number"] == "0100583154"

    def test_line_numbers_are_correct_regardless_of_preamble_length(self, tmp_path):
        """Physical line numbers again: two junk lines, header, data = 4."""
        path = _write(
            tmp_path, "ABC.csv", "sep=,", "title", "Account #,NDC", "0100461951,00310304000"
        )
        assert load_file(path, self._spec()).rows[0].line_number == 4

    def test_a_missing_marker_fails_loudly(self, tmp_path):
        """Falling back to "probably row 0" is how a report title gets read as a
        header and every column binds to nothing."""
        path = _write(tmp_path, "ABC.csv", "Nothing,Like,It", "1,2,3")
        with pytest.raises(HeaderNotFoundError, match="not found"):
            load_file(path, self._spec())

    def test_marker_matching_ignores_case_and_punctuation(self, tmp_path):
        """The marker is matched with the same header normalization as
        everything else, so "ACCOUNT#" finds "Account #"."""
        path = _write(tmp_path, "ABC.csv", "title", "ACCOUNT#,NDC", "0100461951,00310304000")
        assert load_file(path, self._spec()).ok

    def test_a_spec_cannot_declare_both_mechanisms(self):
        """Two answers to "where is the header" is one too many."""
        with pytest.raises(ValueError, match="not both"):
            ReadSpec(header_marker="Account #", preamble_lines=2)
