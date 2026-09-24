import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_specs import (  # noqa: E402
    DATE,
    DROP,
    ISO_DATE,
    MONEY,
    NDC,
    NUM,
    TEXT,
    US_DATE,
    headers_of,
    spec_yaml,
    write,
)

# ---------------------------------------------------------------- ABC
abc_headers = headers_of(
    DROP / "1 Wholesalers" / "ABC_ Purchases Report - Invoice Detail - 01012024-03062026.xlsx",
    "Results",
    preamble=1,
)
abc_over = {
    "account": dict(
        type=TEXT,
        required=True,
        desc="""Leading zeros are significant: 0100461951 here, where the Master
        COGS List writes the same account as 100461951. Text, never integer.""",
    ),
    "invoice_date": dict(type=DATE, required=True, formats=US_DATE),
    "awp_date": dict(type=DATE, formats=US_DATE),
    "invoice": dict(type=TEXT, required=True),
    "ndc": dict(
        type=NDC,
        desc="""Not required. 367 of 4,513 rows in the 2024-2026 extract are
        non-merchandise lines - service charges such as "PHARM DATA SERVICES
        PRICE MAINTENANCE" with a Non-Merchandise amount and no NDC. They are
        real invoice lines carrying real dollars, so they belong in the
        warehouse for invoice-level financial reconciliation; they simply
        cannot take part in NDC-level inventory reconciliation.""",
    ),
    "order_qty": dict(type=NUM),
    "shipped_qty": dict(
        type=NUM,
        required=True,
        control_total=True,
        desc="""Packages, not billing units. Conversion needs BILLUNITSPKG from
        the ASP crosswalk and is reconciliation work; the package figure is
        captured here so that conversion remains possible.""",
    ),
    "invoice_ext_amt": dict(type=MONEY, required=True, control_total=True),
    "non_merchandise": dict(type=MONEY),
    "tax_amount": dict(type=MONEY),
    "current_acq_cost": dict(type=MONEY),
    "total_ext_cost": dict(type=MONEY, control_total=True),
    "awp": dict(type=MONEY),
    "package_awp": dict(type=MONEY),
    "state": dict(pii="quasi"),
    "city": dict(pii="quasi"),
    "customer_pack_size": dict(type=NUM),
    "fdb_package_size_qty": dict(type=NUM),
    "unit_size_qty": dict(type=NUM),
    "fdb_awp_wholesale_factor": dict(type=NUM),
}

ABC_HEADER = """# AmerisourceBergen purchases, invoice detail.
#
# The same 26 columns arrive two ways, and the preamble differs between them:
# as a workbook (sheet "Results") with one report-title row above the header,
# and as a CSV with a `sep=,` declaration AND a title - two lines.
#
# A fixed preamble_lines cannot serve both, and two variants cannot be told
# apart because the headers are identical, so every delivery would have raised
# AmbiguousVariantError. The header is located by header_marker instead: the
# first row carrying a column called "Account #". Still declarative, and a
# missing marker fails loudly rather than reading the title row as a header.
#
# No 2023 data exists for this source - the delivery catalogue records "no 2023
# data" - so coverage_start stops that being reported as a freshness gap."""

ABC_NOTES = """Account numbers carry significant leading zeros (0100461951).
The Master COGS List writes the same accounts without them, so account matching
needs an explicit normalization rule rather than an incidental one.

ABC ships no invoice line number, so (account, invoice, ndc) is the widest key
available and it is not quite unique - the earlier validation pack found two
duplicate rows on that key. Row identity therefore falls back to the row hash,
and the duplicates are a real question for the vendor rather than something to
paper over."""

write(
    "wholesaler.abc.purchases.yaml",
    spec_yaml(
        key="wholesaler.abc.purchases",
        name="AmerisourceBergen - Purchases Invoice Detail",
        vendor="AmerisourceBergen",
        group="1 Wholesalers",
        catalog_names=["Abc"],
        coverage_start="2024-01-01",
        filename_patterns=[r"abc.*purchases"],
        natural_key=["account", "invoice", "ndc"],
        header=ABC_HEADER,
        notes=ABC_NOTES,
        variants=[
            dict(
                name="invoice_detail",
                require_all=["Account #", "NDC", "Shipped Qty"],
                read={"header_marker": "Account #", "sheet": "Results"},
                description="""26-column line-level purchase detail. The same
                column set as a CSV and as a workbook; only the preamble differs.""",
                headers=abc_headers,
                overrides=abc_over,
            )
        ],
    ),
)

# ------------------------------------------------- McKesson Purchasing Detail
mck_inv = headers_of(
    DROP / "1 Wholesalers" / "McKesson_Purchasing Detail - Invoice Level 01012023-03062026.csv"
)
mck_inv_over = {
    "account_number": dict(type=TEXT, required=True),
    "primary_account_number": dict(type=TEXT),
    "invoice_credit_number": dict(type=TEXT, required=True),
    "invoice_credit_date": dict(type=DATE, required=True, formats=ISO_DATE),
    "ndc": dict(type=NDC),
    "mckesson_item_number": dict(type=TEXT, required=True),
    "extended_price": dict(type=MONEY, control_total=True),
    "net_quantity": dict(type=NUM, control_total=True),
    "ordered_quantity": dict(type=NUM, control_total=True),
    "invoice_quantity_filled": dict(type=NUM, control_total=True),
    "returned_credited_quantity": dict(type=NUM, control_total=True),
    "gross_purchases": dict(type=MONEY, control_total=True),
    "gross_credits_and_returns": dict(type=MONEY, control_total=True),
    "net_handling_charge_amount": dict(type=MONEY, control_total=True),
    "net_returned_credited_price": dict(type=MONEY, control_total=True),
    "invoiced_price_per_dose": dict(
        type=MONEY,
        desc="A rate, not an extended amount - deliberately not a control total.",
    ),
    "invoiced_dose_quantity_extended": dict(type=NUM, control_total=True),
}

MCK_INV_HEADER = """# McKesson purchasing detail, invoice level.
#
# THIS FILE HAS A TOTALS FOOTER. Line 6165 of the 2023-2026 extract carries 11
# of 45 cells: Extended Price $20,394,456.72, Net Quantity 11,378, Ordered
# Quantity 14,155, Gross Purchases $20,479,715.99, Gross Credits and Returns
# -$85,259.27. Those are the figures the earlier validation pack reported as
# "$20.39M extended price, 11,378 net units" - it was reading the footer rather
# than the data.
#
# Landing it would add a phantom $20.4M purchase. Declared instead, so its
# figures become an independent control total: arithmetic McKesson performed,
# checked against arithmetic we performed.
#
# The sibling report, McKesson Custom, has NO footer - its last row is real
# data. The two differ, which is exactly the kind of thing that gets assumed
# rather than checked.
#
# The export opens with an unnamed row-number column, excluded from matching as
# an artifact and verified to carry no data any spec needs."""

write(
    "wholesaler.mckesson.invoice.yaml",
    spec_yaml(
        key="wholesaler.mckesson.invoice",
        name="McKesson - Purchasing Detail, Invoice Level",
        vendor="McKesson",
        group="1 Wholesalers",
        catalog_names=["Mckesson"],
        filename_patterns=[r"mckesson.*purchasing detail"],
        natural_key=["invoice_credit_number", "mckesson_item_number"],
        header=MCK_INV_HEADER,
        variants=[
            dict(
                name="invoice_level",
                require_all=[
                    "Invoice/Credit Number",
                    "McKesson Item Number",
                    "Extended Price ($)",
                ],
                read={"footer": "totals_row"},
                headers=mck_inv,
                overrides=mck_inv_over,
            )
        ],
    ),
)

# ------------------------------------------------------ McKesson Custom (R&S)
mck_cust = headers_of(DROP / "1 Wholesalers" / "McKesson_Custom Report 01012023-03062026.csv")
mck_cust_over = {
    "account_number": dict(type=TEXT, required=True),
    "invoice_credit_number": dict(type=TEXT, required=True),
    "invoice_credit_date": dict(type=DATE, required=True, formats=ISO_DATE),
    "wac_effective_date_current_date": dict(type=DATE, formats=ISO_DATE),
    "ndc_current": dict(
        type=NDC,
        desc="""The canonical 11-digit form. Not required: 63 rows carry no NDC
        at all, the same non-drug invoice lines that ABC shows.""",
    ),
    "n10_digit_ndc_current": dict(
        type=TEXT,
        desc="""The vendor's 10-digit rendering of the same NDC, kept as text.

        Deliberately NOT type ndc. An unhyphenated 10-digit NDC cannot be
        resolved without a directory lookup, so typing it as an NDC quarantines
        every row in the file - which is exactly what happened on the first run
        of this spec, 6,102 rows of 6,165.

        It is kept because the pairing with the 11-digit column is the ground
        truth that proved the previous zfill(11) normalization wrong on 55 of
        101 distinct NDCs - see tests/fixtures/ndc_ground_truth_mckesson.json.""",
    ),
    "associated_340b_id": dict(
        type=TEXT,
        desc="""The covered-entity key (OPAID). Present here, in Pharmaforce and
        in Wellpartner, and it is what ties an account to a covered entity.""",
    ),
    "mckesson_item_number": dict(type=TEXT, required=True),
    "po_number": dict(type=TEXT),
    "awp_current": dict(type=MONEY),
    "price_per_package": dict(type=MONEY),
    "standard_suggested_retail_price": dict(type=MONEY),
    "extended_price": dict(type=MONEY, control_total=True),
    "invoiced_price_per_dose": dict(
        type=MONEY,
        desc="A rate, not an extended amount - deliberately not a control total.",
    ),
    "net_purchases": dict(type=MONEY, control_total=True),
    "gross_purchases": dict(type=MONEY, control_total=True),
    "net_quantity": dict(type=NUM, control_total=True),
    "ordered_quantity": dict(type=NUM, control_total=True),
    "received_quantity": dict(type=NUM),
    "total_ordered_quantity": dict(type=NUM),
    "package_size": dict(type=NUM),
    "package_size_multiplier": dict(
        type=NUM,
        desc="""Packages to units. An input to unit conversion - captured now,
        applied when reconciliation is built.""",
    ),
    "average_quantity_per_invoice_pkg": dict(type=NUM),
    "invoice_quantity_omitted": dict(type=NUM),
    "invoice_quantity_filled": dict(type=NUM),
}

MCK_CUST_HEADER = """# McKesson custom purchasing report - listed in the delivery catalogue as
# "R and S", which nothing else would tell you.
#
# Carries both the 10-digit and the 11-digit NDC on every row. That pairing is
# the ground truth that proved the previous zfill(11) normalization wrong on 55
# of 101 distinct NDCs, and it is committed as a regression fixture.
#
# Also carries Associated 340B ID (OPAID) - the covered-entity key that links an
# account to a covered entity, shared with Pharmaforce and Wellpartner.
#
# NO totals footer, unlike the sibling Purchasing Detail report. Verified: the
# last row is real data.
#
# Opens with an unnamed row-number column, excluded from matching as an
# artifact."""

write(
    "wholesaler.mckesson.custom.yaml",
    spec_yaml(
        key="wholesaler.mckesson.custom",
        name="McKesson - Custom Report (R and S)",
        vendor="McKesson",
        group="1 Wholesalers",
        catalog_names=["R and S"],
        filename_patterns=[r"mckesson.*custom"],
        natural_key=["invoice_credit_number", "mckesson_item_number", "po_number"],
        header=MCK_CUST_HEADER,
        variants=[
            dict(
                name="custom_report",
                require_all=[
                    "Invoice/Credit Number",
                    "NDC (Current)",
                    "Associated 340B ID",
                ],
                headers=mck_cust,
                overrides=mck_cust_over,
            )
        ],
    ),
)
