import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_specs import (  # noqa: E402
    DATE,
    DROP,
    MONEY,
    NDC,
    NUM,
    TEXT,
    US_DATE,
    headers_of,
    spec_yaml,
    write,
)

T = DROP / "4 TPA"

pf = headers_of(T / "PharmaForce_Claims Fire Mesa 01012023-03062026.csv")
prxp = headers_of(T / "PRXP_Rx Transaction Details 01012023-03062026.xlsx")
wp_claims = headers_of(T / "Wellpartner_Claims 01012023-03062026.xlsx")
wp_inv = headers_of(T / "Wellpartner_WholesalerInvoices 01012023-03062026.xlsx")

CE_DESC = """The covered-entity key. Two exist in this data: STD891281 (Fire
Mesa, Nevada) and STD06489 (Southington, Connecticut). The same identifier
appears as OPAID here, as 340B ID in Wellpartner Claims, embedded in Entity Name
in Wellpartner Invoices, and as Associated 340B ID in the McKesson Custom
report - which makes it the one key that ties every feed to a covered entity."""

# ------------------------------------------------------------- Pharmaforce
PF_OVER = {
    "claim_id": dict(type=TEXT, required=True),
    "opaid": dict(type=TEXT, required=True, desc=CE_DESC),
    "covered_entity": dict(type=TEXT, required=True),
    "ndc": dict(type=NDC),
    "fill_date": dict(type=DATE, required=True, formats=US_DATE),
    "claim_date": dict(type=DATE, formats=US_DATE),
    "date_rx_written": dict(type=DATE, formats=US_DATE),
    "claim_captured_date": dict(type=DATE, formats=US_DATE),
    "encounter_visit_date": dict(type=DATE, formats=US_DATE),
    "encounter_discharge_date": dict(type=DATE, formats=US_DATE),
    "e_prescribe_order_date": dict(type=DATE, formats=US_DATE),
    "trued_up_date": dict(type=DATE, formats=US_DATE),
    "dob": dict(type=DATE, formats=US_DATE, pii="direct"),
    "first_name": dict(pii="direct"),
    "last_name": dict(pii="direct"),
    "gender": dict(pii="quasi"),
    "patient_id": dict(type=TEXT, pii="join_key"),
    "medical_record": dict(type=TEXT, pii="join_key"),
    "prescriber_name": dict(type=TEXT),
    "prescriber_npi_dea": dict(type=TEXT),
    "prescription": dict(type=TEXT),
    "qty_dispensed": dict(
        type=NUM,
        control_total=True,
        desc="""Negative on roughly 8.7% of rows - 17,052 of 194,907 in the
        2023-2026 extract. The earlier validation pack flagged this as a possible
        integrity problem because it assumed reversals arrive as separate claims.
        They do not: Pharmaforce writes a reversal as a negative quantity on the
        same claim shape, so the negatives are the reversal mechanism and must be
        summed, not filtered. Confirm with the Pharmaforce account team before
        any roll-up relies on it.""",
    ),
    "days_supply": dict(type=NUM),
    "package_size": dict(type=NUM),
    "refill": dict(type=TEXT),
    "patient_pay": dict(type=MONEY, control_total=True),
    "third_party_payment": dict(type=MONEY, control_total=True),
    "total_payment": dict(type=MONEY, control_total=True),
    "disp_fee": dict(type=MONEY, control_total=True),
    "ce_receivable": dict(
        type=MONEY,
        control_total=True,
        desc="""What the covered entity is owed. Distinct from Profit OR Loss -
        the previous financial view used one Wellpartner field for both
        receivable and profit, which is a defect this separation avoids.""",
    ),
    "n340b_drug_cost": dict(type=MONEY, control_total=True),
    "total_claim_cost": dict(type=MONEY, control_total=True),
    "profit_or_loss": dict(type=MONEY, control_total=True),
    "retail_drug_cost": dict(type=MONEY, control_total=True),
    "trued_up_units": dict(type=NUM, control_total=True),
    "trued_up_cost": dict(type=MONEY, control_total=True),
    "replenishment_status": dict(type=TEXT),
    "billing_model": dict(type=TEXT),
}

PF_HEADER = """# Pharmaforce 340B claims.
#
# ONE spec for both delivery files. Fire Mesa and Southington arrive as separate
# extracts with the same 65 columns, and Claim ID is unique across both - 196,187
# rows, 196,187 distinct ids, zero collisions - so they are one source, not two.
#
# The previous implementation ran one parser class twice with a location string
# injected from configuration. That is unnecessary: every row already carries
# OPAID and Covered Entity, so the entity comes from the data rather than from a
# setting that can drift out of step with the file it describes.
#
# Two covered entities exist in this data: STD891281 (Fire Mesa, Nevada) and
# STD06489 (Southington, Connecticut). Southington contributes 1,280 rows to Fire
# Mesa's 194,907, consistent with its 340B participation ending on 2025-07-17.
#
# Qty Dispensed is negative on about 8.7% of rows. Those are reversals, written
# as negative quantities on the same claim shape rather than as separate reversal
# claims - see the column note."""

write(
    "tpa.pharmaforce.claims.yaml",
    spec_yaml(
        key="tpa.pharmaforce.claims",
        name="Pharmaforce - 340B Claims",
        vendor="Pharmaforce",
        group="4 TPA",
        catalog_names=["Pharmaforce - Firemesa", "Pharmaforce - Southington"],
        filename_patterns=[r"pharmaforce.*claim"],
        natural_key=["claim_id"],
        header=PF_HEADER,
        variants=[
            dict(
                name="claims_65",
                description="""65 columns, identical for both site deliveries.""",
                require_all=["Claim ID", "OPAID", "Covered Entity"],
                headers=pf,
                overrides=PF_OVER,
            )
        ],
    ),
)

# --------------------------------------------------------------------- PRXP
PRXP_OVER = {
    "rx_transaction_id": dict(type=TEXT, required=True),
    "covered_entity_name": dict(type=TEXT),
    "facility_name": dict(type=TEXT),
    "ndc": dict(type=NDC),
    "qty_dispensed": dict(type=NUM, control_total=True),
    "days_supply": dict(type=NUM),
    "total_amount_collected": dict(type=MONEY, control_total=True),
}
for name in prxp:
    t = name.strip().lower()
    if any(w in t for w in ("date", "dob")):
        key = (
            name.strip()
            .lower()
            .replace(" ", "_")
            .replace("#", "")
            .replace("(", "")
            .replace(")", "")
            .replace("/", "_")
            .replace("-", "_")
            .strip("_")
        )
        PRXP_OVER.setdefault(key, dict(type=DATE, formats=US_DATE))
    if any(
        w in t for w in ("first name", "last name", "patient name", "address", "phone", "email")
    ):
        key = name.strip().lower().replace(" ", "_").replace("#", "").strip("_")
        PRXP_OVER.setdefault(key, dict(pii="direct"))

PRXP_HEADER = """# PRXP rx transaction details.
#
# Natural key: Rx Transaction ID, unique across all 5,133 rows - the one source
# the earlier validation pack already had right.
#
# 90 columns in the 2023-2026 extract and 94 in the later drops. The extra four
# have not been reconciled against a spec yet, so on_new_column is set to warn
# rather than fail: a newer delivery loads and the additions are reported, rather
# than the whole file being refused. Tighten this to fail once the added columns
# are known and mapped."""

write(
    "tpa.prxp.transactions.yaml",
    spec_yaml(
        key="tpa.prxp.transactions",
        name="PRXP - Rx Transaction Details",
        vendor="PRXP",
        group="4 TPA",
        catalog_names=["Prxp"],
        filename_patterns=[r"prxp.*transaction"],
        natural_key=["rx_transaction_id"],
        header=PRXP_HEADER,
        on_new_column="warn",
        variants=[
            dict(
                name="transactions_90",
                description="""90 columns, the 2023-2026 shape.""",
                require_all=["Rx Transaction ID"],
                headers=prxp,
                overrides=PRXP_OVER,
            )
        ],
    ),
)

# --------------------------------------------------------- Wellpartner claims
WP_OVER = {
    "transaction": dict(type=TEXT, required=True),
    "n340b_id": dict(type=TEXT, required=True, desc=CE_DESC),
    "entity_name": dict(type=TEXT, required=True),
    "state_of_ce": dict(type=TEXT, pii="quasi"),
    "ndc": dict(type=NDC),
    "processed_date": dict(type=DATE, formats=US_DATE),
    "dispensed_date": dict(type=DATE, required=True, formats=US_DATE),
    "rx_written_date": dict(type=DATE, formats=US_DATE),
    "batch_submission_date": dict(type=DATE, formats=US_DATE),
    "qualification_date": dict(type=DATE, formats=US_DATE),
    "patient_dob": dict(type=DATE, formats=US_DATE, pii="direct"),
    "patient_qualification_dob": dict(type=DATE, formats=US_DATE, pii="direct"),
    "patient_name": dict(pii="direct"),
    "patient_qualification_first_name": dict(pii="direct"),
    "patient_qualification_last_name": dict(pii="direct"),
    "patient_mrn": dict(type=TEXT, pii="join_key"),
    "rx": dict(type=TEXT),
    "fill": dict(type=TEXT),
    "dispensed_qty": dict(type=NUM, control_total=True),
    "packages_dispensed": dict(type=NUM, control_total=True),
    "days_supply": dict(type=NUM),
    "mdq": dict(type=NUM),
    "program_revenue": dict(type=MONEY, control_total=True),
    "patient_paid": dict(type=MONEY, control_total=True),
    "est_340b_cost": dict(type=MONEY, control_total=True),
    "est_wholesale_cost": dict(type=MONEY, control_total=True),
    "admin_fee": dict(type=MONEY, control_total=True),
    "dispensing_fee": dict(type=MONEY, control_total=True),
    "entity_subsidy": dict(type=MONEY, control_total=True),
    "est_entity_net_savings": dict(
        type=MONEY,
        control_total=True,
        desc="""Estimated savings, and only that. The previous financial view
        used this single field as both the entity receivable and the profit
        figure, which double-counts one number into two meanings. Program
        Revenue, Entity Subsidy and Est 340B Cost are separate columns and should
        stay separate.""",
    ),
    "carve_in_model": dict(type=TEXT),
    "carve_in_method": dict(type=TEXT),
    "replenishment_model_type": dict(type=TEXT),
    "transaction_status": dict(type=TEXT),
    "transaction_type": dict(type=TEXT),
    "prescriber_npi": dict(type=TEXT),
    "prescriber_name": dict(type=TEXT),
}

WP_HEADER = """# Wellpartner 340B claims, from the Claims tab.
#
# Natural key: Transaction #, unique across all 42,018 rows. The wider candidate
# (Rx # + Dispensed Date + NDC + Fill #) is also unique, and is a useful check if
# the transaction number is ever absent.
#
# Carries the covered-entity key twice: as 340B ID and again inside Entity Name.
# Both STD891281 and STD06489 appear.
#
# Est Entity Net Savings is one field with one meaning. The previous financial
# view used it as both the entity receivable and the profit figure; the columns
# it should have used - Program Revenue, Entity Subsidy, Est 340B Cost - are all
# present separately."""

write(
    "tpa.wellpartner.claims.yaml",
    spec_yaml(
        key="tpa.wellpartner.claims",
        name="Wellpartner - 340B Claims",
        vendor="Wellpartner",
        group="4 TPA",
        catalog_names=["Wellpartner (Claims tab)"],
        filename_patterns=[r"wellpartner.*claim"],
        natural_key=["transaction"],
        header=WP_HEADER,
        variants=[
            dict(
                name="claims_119",
                require_all=["Transaction #", "340B ID", "Entity Name"],
                headers=wp_claims,
                overrides=WP_OVER,
            )
        ],
    ),
)

# ------------------------------------------------------- Wellpartner invoices
WPI_OVER = {
    "entity_name": dict(
        type=TEXT,
        required=True,
        desc="""Embeds the covered-entity key, as "Sagebrush Health Services
        (STD891281)". Ten rows carry it as "(STD06489 )" with a trailing space
        inside the parenthesis, which is what makes them look like duplicate
        invoices - see the note on this source.""",
    ),
    "wholesaler_invoice_date": dict(type=DATE, required=True, formats=US_DATE),
    "wholesaler_name": dict(type=TEXT, required=True),
    "wholesaler_account": dict(type=TEXT, required=True),
    "wholesaler_invoice": dict(type=TEXT, required=True),
    "invoice_net_amount": dict(type=MONEY, required=True, control_total=True),
    "purchase_order": dict(type=TEXT),
    "contract_pharmacy_name": dict(type=TEXT),
    "hub_spoke": dict(type=TEXT),
    "invoice_type": dict(type=TEXT, desc="Debit or Credit."),
}

WPI_HEADER = """# Wellpartner wholesaler invoices, from the Inventory Activity tab.
#
# The earlier validation pack reported 10 duplicate rows here and could not find
# a key that separated them. They are not distinct records: all ten pairs are the
# same invoice written twice, differing only in the entity name -
# "Sagebrush Health Services (STD06489 )" against "(STD06489)", a trailing space
# inside the parenthesis. Ten of 7,700 rows.
#
# So the natural key is the real business key - invoice number, wholesaler,
# account and date - and the ten pairs are genuine duplicates for the ledger to
# collapse, not a signal that the key is too narrow. Extracting the 340B ID with
# a pattern rather than matching the entity name as a string avoids the whole
# class of problem.
#
# This is a second, independent view of wholesaler purchases: Wellpartner
# reporting what it was invoiced. It is not the same as the wholesaler extracts
# and must not be unioned with them."""

write(
    "tpa.wellpartner.invoices.yaml",
    spec_yaml(
        key="tpa.wellpartner.invoices",
        name="Wellpartner - Wholesaler Invoices",
        vendor="Wellpartner",
        group="4 TPA",
        catalog_names=["Wellpartner Wholesaler Invoices (under Inventory activity)"],
        filename_patterns=[r"wellpartner.*wholesalerinvoice"],
        natural_key=[
            "wholesaler_invoice",
            "wholesaler_name",
            "wholesaler_account",
            "wholesaler_invoice_date",
        ],
        header=WPI_HEADER,
        variants=[
            dict(
                name="invoices_10",
                require_all=["Wholesaler Invoice #", "Invoice Net Amount", "Hub-Spoke"],
                headers=wp_inv,
                overrides=WPI_OVER,
            )
        ],
    ),
)
