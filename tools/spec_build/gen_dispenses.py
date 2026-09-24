import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_specs import (  # noqa: E402
    DATE,
    DROP,
    MONEY,
    NDC,
    NEW_DROP,
    NUM,
    TEXT,
    US_DATE,
    ISO_DATE,
    headers_of,
    spec_yaml,
    write,
)

DIS = DROP / "2 Dispenses"
NDIS = NEW_DROP / "2 Dispenses"

adm_old = headers_of(DIS / "WeInfuse_AdministeredDrugsReport 01012023-03062026.csv")
adm_new = headers_of(NDIS / "WeInfuse_AdministeredDrugsReport 03012026-06102026.xlsx")
buybill = headers_of(DIS / "WeInfuse_BuyBill-AdministeredDrugsReport 01012023-03062026.csv")
pat_old = headers_of(DIS / "WeInfuse PatientsReport 01012023-03062026.csv")
pat_new = headers_of(NDIS / "WeInfuse PatientsReport 03012026-06102026.xlsx")

# The new reporting module renamed 14 columns. Both land on one canonical name
# so downstream code never learns which module produced a row.
ADM_RENAME = {
    "Inner NDC Code": "inner_ndc",
    "Outer NDC Code": "outer_ndc",
    "HCPCS": "billing_code_hcpcs",
    "Location Name": "location",
    "Appointment Date": "administration_date",
    "Appointment Time": "administration_time",
    "Created Date": "created_at",
    "Expiration Date": "expiration",
    "Price": "unit_price",
    "Primary Insurance": "primary_insurance_plan",
    "Secondary Insurance": "secondary_insurance_plan",
    "Tertiary Insurance": "tertiary_insurance_plan",
    "Inventory Type": "inventory_type",
    "Date of Birth Date": "date_of_birth",
}
ADM_RENAME_OLD = {
    "Date": "administration_date",
    "Time": "administration_time",
    "Type": "inventory_type",
}

ADM_ALIASES = {
    "inner_ndc": ["Inner NDC", "Inner NDC Code"],
    "outer_ndc": ["Outer NDC", "Outer NDC Code"],
    "billing_code_hcpcs": ["Billing Code (HCPCS)", "HCPCS"],
    "location": ["Location", "Location Name"],
    "administration_date": ["Date", "Appointment Date"],
    "administration_time": ["Time", "Appointment Time"],
    "created_at": ["Created At", "Created Date"],
    "expiration": ["Expiration", "Expiration Date"],
    "unit_price": ["Unit Price", "Price"],
    "primary_insurance_plan": ["Primary Insurance Plan", "Primary Insurance"],
    "secondary_insurance_plan": ["Secondary Insurance Plan", "Secondary Insurance"],
    "tertiary_insurance_plan": ["Tertiary Insurance Plan", "Tertiary Insurance"],
    "weinfuse_uid": ["Weinfuse UID", "WeInfuse UID"],
}

ADM_OVER = {
    "order_id": dict(type=TEXT, required=True),
    "inventory_item_id": dict(
        type=TEXT,
        required=True,
        desc="""The discriminator that makes a row unique. One appointment
        administers several inventory items, which is why keys built on
        appointment alone showed 25,738 duplicates in 51,178 rows.""",
    ),
    "appointment_id": dict(type=TEXT),
    "order_series_id": dict(type=TEXT),
    "weinfuse_uid": dict(type=TEXT, pii="join_key"),
    "patient_identifier": dict(type=TEXT, pii="join_key"),
    "patient_first_name": dict(pii="direct"),
    "patient_last_name": dict(pii="direct"),
    "date_of_birth": dict(type=DATE, formats=US_DATE, pii="direct"),
    "provider_first_name": dict(type=TEXT),
    "provider_last_name": dict(type=TEXT),
    "provider_npi": dict(type=TEXT),
    "group_id": dict(type=TEXT),
    "location_id": dict(
        type=TEXT,
        desc="""The site key. The 340B verification plan requires this to
        attribute a dispense to a clinic, because ABC account 0100583154 is
        shared across all three clinics and Cardinal 2057234776 across two -
        account number alone cannot separate them.""",
    ),
    "inner_ndc": dict(type=NDC),
    "outer_ndc": dict(
        type=NDC,
        desc="""The package NDC, where inner_ndc is the unit. Both captured; the
        pair is an input to package-to-billing-unit conversion.""",
    ),
    "billing_code_hcpcs": dict(type=TEXT),
    "billing_units": dict(
        type=NUM,
        control_total=True,
        desc="""HCPCS billing units, NOT packages. Wholesalers ship packages, so
        no comparison between the two is valid without multiplying purchases by
        BILLUNITSPKG from the ASP crosswalk. That conversion is reconciliation
        work; this is the figure it starts from.""",
    ),
    "unit_price": dict(type=MONEY, desc="A rate - deliberately not a control total."),
    "strength": dict(type=NUM),
    "administration_date": dict(type=DATE, required=True, formats=US_DATE),
    "expiration": dict(type=DATE, formats=US_DATE),
    "inventory_type": dict(
        type=TEXT,
        desc="""Called Type in the legacy export and Inventory Type in the new
        module, and it carried the vendor name (Henry Schein, Curascript) in the
        legacy form. Worth checking before it is used as a channel signal.""",
    ),
}

ADM_HEADER = """# WeInfuse administered drugs - clinic administration events.
#
# Two variants, because WeInfuse moved to a new reporting module in the June
# 2026 drop. The export went from 33 CSV columns to 35 XLSX ones and renamed 14
# of them: Inner NDC to Inner NDC Code, Billing Code (HCPCS) to HCPCS, Date to
# Appointment Date, Unit Price to Price, and so on. Both variants land on one
# set of canonical names, so nothing downstream learns which module wrote a row.
#
# NATURAL KEY. (Order ID, Inventory Item ID) is unique across all 51,178 rows of
# the 2023-2026 extract. The earlier validation pack recorded this source as
# "candidate key unknown - best returned 25,738 duplicates out of 51,178"; it
# had been testing Weinfuse UID plus Appointment ID. Inventory Item ID is the
# discriminator, because one appointment administers several inventory items.
#
# The legacy Administered export and the BuyBill export are nearly identical -
# 33 columns each, differing only in Location/Type versus Location Name/Vendor.
# The detectors below use exactly those columns to keep them apart, since
# reading one as the other would mix medical-benefit administrations with
# buy-and-bill ones."""

write(
    "dispense.weinfuse.administered.yaml",
    spec_yaml(
        key="dispense.weinfuse.administered",
        name="WeInfuse - Administered Drugs",
        vendor="WeInfuse",
        group="2 Dispenses",
        catalog_names=["Weinfuses - Administered Drug"],
        filename_patterns=[r"weinfuse.*administereddrugs(?!.*buybill)", r"weinfuse_administered"],
        natural_key=["order_id", "inventory_item_id"],
        header=ADM_HEADER,
        variants=[
            dict(
                name="reporting_module_2026",
                description="""35-column workbook from the reporting module
                introduced in the June 2026 drop. Adds Inventory Type and Date of
                Birth Date; renames 14 columns.""",
                require_all=["Inventory Type", "Inner NDC Code", "Billing Units"],
                headers=adm_new,
                rename=ADM_RENAME,
                aliases=ADM_ALIASES,
                overrides=ADM_OVER,
            ),
            dict(
                name="legacy_export",
                description="""33-column CSV export used through the March 2026
                drop. Distinguished from BuyBill by carrying Type and Location
                where BuyBill carries Vendor and Location Name.""",
                require_all=["Type", "Inner NDC", "Billing Units"],
                require_none=["Inventory Type", "Vendor"],
                headers=adm_old,
                rename=ADM_RENAME_OLD,
                aliases=ADM_ALIASES,
                overrides=ADM_OVER,
            ),
        ],
    ),
)

# ------------------------------------------------------------------ BuyBill
BB_OVER = dict(ADM_OVER)
BB_OVER["vendor"] = dict(
    type=TEXT,
    desc="""Supplier of the administered item - Henry Schein, Curascript. This
    is the only place CuraScript appears in any ingested feed; its purchase data
    is not obtainable ("no access on reporting tool"), which is why the 340B
    verification plan treats CSD-routed replenishment as a known gap.""",
)

BB_HEADER = """# WeInfuse buy-and-bill administered drugs.
#
# DISCONTINUED. The delivery catalogue for the June 2026 drop records this
# source as "not in the new reporting module", and no file arrived in that drop.
# Its 45,013 historical rows stay in the warehouse and remain reconcilable; the
# lifecycle marking simply stops its absence being raised as a freshness alert
# every week from now on.
#
# This matters more than the row count suggests: the 340B verification plan
# scoped its entire reconciliation to dbo.weinfuse_buybill_administered. The
# feed that work was built on has stopped, and whatever replaces it needs
# deciding before buy-and-bill reconciliation can be brought forward.
#
# Natural key (Order ID, Inventory Item ID) is unique across all 45,013 rows.
# The earlier validation pack recorded "2 candidates, both returned 22,173
# duplicates out of 45,013"."""

write(
    "dispense.weinfuse.buybill.yaml",
    spec_yaml(
        key="dispense.weinfuse.buybill",
        name="WeInfuse - Buy and Bill Administered Drugs",
        vendor="WeInfuse",
        group="2 Dispenses",
        catalog_names=["Weinfuses - BuyBill Administered Drug"],
        lifecycle="discontinued",
        cadence=None,
        filename_patterns=[r"weinfuse.*buybill"],
        natural_key=["order_id", "inventory_item_id"],
        header=BB_HEADER,
        variants=[
            dict(
                name="legacy_export",
                description="""33 columns. Distinguished from the legacy
                Administered export by carrying Vendor and Location Name where
                that one carries Type and Location.""",
                require_all=["Vendor", "Inner NDC", "Billing Units"],
                require_none=["Inventory Type", "Type"],
                headers=buybill,
                aliases=ADM_ALIASES,
                overrides=BB_OVER,
            )
        ],
    ),
)

# ----------------------------------------------------------------- Patients
PAT_RENAME_NEW = {
    "Patient ID": "patient_identifier",
    "DOB": "date_of_birth",
    "Group": "group_name",
    "Location": "location_name",
    "Emergency Contact": "emergency_contact_name",
    "Created Date": "last_updated",
}
PAT_ALIASES = {
    "patient_identifier": ["Patient Identifier", "Patient ID"],
    "date_of_birth": ["Date of Birth", "DOB"],
    "group_name": ["Group Name", "Group"],
    "location_name": ["Location Name", "Location"],
    "emergency_contact_name": ["Emergency Contact Name", "Emergency Contact"],
    "last_updated": ["Last Updated", "Created Date"],
    "weinfuse_uid": ["Weinfuse UID", "WeInfuse UID"],
}
# WeInfuse writes two different temporal shapes in one file: plain ISO dates
# for anything a human entered (date of birth, insurance effective date) and
# UTC timestamps for anything the system stamped (last updated, entered at).
# Declaring the wrong one quarantined all 1,918 rows on the first run, which is
# the intended behaviour - a format that no longer matches must stop the load
# rather than be reinterpreted.
UTC_STAMP = ["%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M:%S"]

PAT_OVER = {
    "weinfuse_uid": dict(type=TEXT, pii="join_key"),
    "patient_identifier": dict(type=TEXT, required=True, pii="join_key"),
    "group_id": dict(type=TEXT),
    "location_id": dict(type=TEXT),
    "first_name": dict(pii="direct"),
    "last_name": dict(pii="direct"),
    "middle_initial": dict(pii="direct"),
    "patient_name": dict(pii="direct"),
    "date_of_birth": dict(type=DATE, formats=ISO_DATE, pii="direct"),
    "email": dict(pii="direct"),
    "mobile_phone": dict(pii="direct"),
    "work_phone": dict(pii="direct"),
    "home_phone": dict(pii="direct"),
    "address_line_1": dict(pii="direct"),
    "address_line_2": dict(pii="direct"),
    "emergency_contact_name": dict(pii="direct"),
    "emergency_contact_phone": dict(pii="direct"),
    "city": dict(pii="quasi"),
    "state": dict(pii="quasi"),
    "zip_code": dict(pii="quasi"),
    "sex": dict(pii="quasi"),
    "ethnicity": dict(pii="quasi"),
    "race": dict(pii="quasi"),
    "preferred_language": dict(pii="quasi"),
    "last_updated": dict(type=DATE, formats=UTC_STAMP),
    "primary_insurance_effective_date": dict(type=DATE, formats=ISO_DATE),
    "secondary_insurance_effective_date": dict(type=DATE, formats=ISO_DATE),
    "tertiary_insurance_effective_date": dict(type=DATE, formats=ISO_DATE),
    "primary_insurance_entered_at": dict(type=DATE, formats=UTC_STAMP),
    "secondary_insurance_entered_at": dict(type=DATE, formats=UTC_STAMP),
    "tertiary_insurance_entered_at": dict(type=DATE, formats=UTC_STAMP),
    "primary_insurance_member_id": dict(pii="direct"),
    "secondary_insurance_member_id": dict(pii="direct"),
    "tertiary_insurance_member_id": dict(pii="direct"),
}

PAT_HEADER = """# WeInfuse patient master - a dimension, not a transaction feed.
#
# The most PHI-dense of the WeInfuse sources: name, date of birth, email, three
# telephone numbers, postal address, emergency contact and three insurance
# member IDs. Direct identifiers are classified so they land encrypted; the
# WeInfuse UID and Patient Identifier stay in clear as join keys.
#
# The new reporting module cut this report almost in half - 48 columns to 23.
# Lost: Weinfuse UID, separate first and last name, Sex, Ethnicity, Race,
# Preferred Language, all insurance detail beyond a plan name, and both Group ID
# and Location ID.
#
# LOCATION ID IS THE LOSS THAT MATTERS. It is the site key the 340B verification
# plan relies on to attribute activity to a clinic, because shared wholesaler
# accounts cannot do it. The new report carries only a location *name*. The
# administered-drugs export still carries Location ID, so dispense attribution
# survives - but if this report is ever the only source of a patient's site,
# that link is now a name match rather than an identifier.
#
# Natural key: Patient Identifier is unique across all 1,918 rows, as is
# Weinfuse UID. Patient Identifier is used because the new module dropped the
# UID entirely.
#
# Two temporal shapes in one file, and they are not interchangeable: plain ISO
# dates for anything a human entered (date of birth, insurance effective date)
# and UTC timestamps for anything the system stamped (last updated, entered at).
# Declaring the wrong one quarantined all 1,918 rows on the first run of this
# spec - which is the intended behaviour, since a format that no longer matches
# must stop the load rather than be silently reinterpreted."""

write(
    "dispense.weinfuse.patients.yaml",
    spec_yaml(
        key="dispense.weinfuse.patients",
        name="WeInfuse - Patients",
        vendor="WeInfuse",
        group="2 Dispenses",
        catalog_names=["Weinfuse - Patients"],
        filename_patterns=[r"weinfuse.*patients"],
        natural_key=["patient_identifier"],
        header=PAT_HEADER,
        variants=[
            dict(
                name="reporting_module_2026",
                description="""23-column workbook from the new reporting module.
                Substantially reduced; notably carries no Location ID.""",
                require_all=["Patient ID", "Status", "Substatus"],
                headers=pat_new,
                rename=PAT_RENAME_NEW,
                aliases=PAT_ALIASES,
                overrides=PAT_OVER,
            ),
            dict(
                name="legacy_export",
                description="""48-column CSV with full demographics and three
                tiers of insurance detail.""",
                require_all=["Patient Identifier", "Primary Insurance Member ID"],
                require_none=["Substatus"],
                headers=pat_old,
                aliases=PAT_ALIASES,
                overrides=PAT_OVER,
            ),
        ],
    ),
)
