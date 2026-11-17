"""The workbook reader's handling of the legacy .xls format (``ingest.reader``)."""

from __future__ import annotations


class TestLegacyWorkbookCells:
    """The old .xls format (the CMS ASP crosswalk, 2026-11-17) stores every
    number as a float. A whole number must come back without the ".0" the
    file never showed, or a HCPCS code of 90371 lands as "90371.0"."""

    def test_a_whole_number_is_an_int_and_the_rest_is_left_alone(self):
        import datetime as dt
        from types import SimpleNamespace as Cell

        import xlrd

        from sbh_data_bank.ingest.reader import _xls_cell

        assert _xls_cell(Cell(ctype=xlrd.XL_CELL_NUMBER, value=90371.0), 0) == 90371
        assert isinstance(_xls_cell(Cell(ctype=xlrd.XL_CELL_NUMBER, value=90371.0), 0), int)
        assert _xls_cell(Cell(ctype=xlrd.XL_CELL_NUMBER, value=0.5), 0) == 0.5
        assert _xls_cell(Cell(ctype=xlrd.XL_CELL_TEXT, value="J0129"), 0) == "J0129"
        assert _xls_cell(Cell(ctype=xlrd.XL_CELL_EMPTY, value=""), 0) is None
        assert _xls_cell(Cell(ctype=xlrd.XL_CELL_BLANK, value=""), 0) is None
        assert _xls_cell(Cell(ctype=xlrd.XL_CELL_DATE, value=46296.0), 0) == dt.datetime(
            2026, 10, 1
        )
