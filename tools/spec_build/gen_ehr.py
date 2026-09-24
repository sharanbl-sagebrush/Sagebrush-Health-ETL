import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_specs import (  # noqa: E402
    DATE,
    DROP,
    MONEY,
    NUM,
    TEXT,
    US_DATE,
    detect_date_formats,
    headers_of,
    sample_values,
    spec_yaml,
    write,
)

EHR = DROP / "3 AllscriptsCurve"
ATH = DROP / "5 Athena"

allscripts = headers_of(EHR / "AllScripts_Prescibe Medication_v2 01012023-03062026.csv")
curve_svc = headers_of(EHR / "Curve_Monthly Services 01012023-03062026.csv")
curve_appt = headers_of(EHR / "Curve_Appointment Details by Date 01012023-03062026.csv")
ath_enc = headers_of(ATH / "Athena_Encounter Report 01012024-03062026.csv", preamble=1)
ath_rx = headers_of(ATH / "Athena_Prescription 01012024-03062026.csv", preamble=1)
ath_elig = headers_of(ATH / "Athena_Patient Eligibility Report as of 03062026.csv", preamble=1)


def apply_detected_dates(overrides, path, headers, preamble=0):
    """Type every column whose values actually parse as dates.

    Value-based, not name-based. A name rule over this estate misses DOB,
    SvcDateFrom, BillDateOrig and two columns called PIF_4 and PIF_5 that hold
    dates and say nothing about it.
    """
    samples = sample_values(path, preamble=preamble)
    for header in headers:
        key = "".join(ch if ch.isalnum() else "_" for ch in header.strip().lower()).strip("_")
        if key in overrides and overrides[key].get("type"):
            continue
        formats = detect_date_formats(samples.get(header, []))
        if formats:
            existing = overrides.setdefault(key, {})
            existing["type"] = DATE
            existing["formats"] = formats
    return overrides


def auto_pii(headers, extra=None):
    """Classify by column name. Every one is checked against the real header
    list below, so nothing is classified that does not exist."""
    out = dict(extra or {})
    for h in headers:
        t = h.strip().lower()
        key = "".join(ch if ch.isalnum() else "_" for ch in t).strip("_").replace("__", "_")
        if "ssn" in t or "social security" in t:
            out.setdefault(key, dict(pii="prohibited"))
        elif any(
            w in t
            for w in ("lname", "fname", "last_name", "first_name", "lastname", "firstname",
                      "dob", "birth", "email", "phone", "street", "address", "middle",
                      "guar_", "patient_name", "emergency")
        ):  # fmt: skip
            out.setdefault(key, dict(pii="direct"))
        elif any(w in t for w in ("city", "state", "zip", "gender", "sex", "marital", "age",
                                  "ethnic", "race", "language")):  # fmt: skip
            out.setdefault(key, dict(pii="quasi"))
        elif any(w in t for w in ("mrn", "patient_number", "patientid", "patient_id", "clientid")):
            out.setdefault(key, dict(type=TEXT, pii="join_key"))
    return out


# ----------------------------------------------------------- Allscripts eRx
ALS_OVER = auto_pii(
    allscripts,
    {
        "ordered_date": dict(type=DATE, required=True, formats=US_DATE),
        "dob": dict(type=DATE, formats=US_DATE, pii="direct"),
        "qty": dict(type=NUM),
        "imremed_code": dict(type=TEXT),
        "imreenc_code": dict(type=TEXT),
        "npi": dict(type=TEXT),
        "dem_externalid": dict(type=TEXT, pii="join_key"),
        "location_name1": dict(
            type=TEXT,
            desc="""Both LOCATION_NAME1 and LOCATION_NAME exist in this file.
            The previous parser carried a bespoke `or` fallback for exactly this
            pair; here they are simply two distinct columns, mapped separately.""",
        ),
    },
)

ALS_NOTES = """No natural key exists in the delivered columns. Candidates tested
against all 132,974 rows of the 2023-2026 extract: IMREMED_CODE (87,824
duplicate rows), IMREENC_CODE + IMREMED_CODE (14,683), the same plus Refill
(14,325), and DEM_EXTERNALID + ORDERED_DATE + IMREMED_CODE (16,250).

Worse, 8,100 rows are exact duplicates across every column, so even the row hash
cannot separate them. This is a denormalised report extract rather than a
transaction log, and it needs snapshot semantics at ledger time: a delivery
replaces its date range rather than being merged into it. Asking Allscripts for
an order identifier would remove the problem entirely and is worth doing."""

write(
    "ehr.allscripts.erx.yaml",
    spec_yaml(
        key="ehr.allscripts.erx",
        name="Allscripts - Prescribe Medication (eRx)",
        vendor="Allscripts",
        group="3 AllscriptsCurve",
        catalog_names=["ERX - Prescribe Medication"],
        filename_patterns=[r"allscripts.*prescibe", r"allscripts.*prescribe"],
        natural_key=[],
        notes=ALS_NOTES,
        header="""# Allscripts eRx - prescribed medication orders.
#
# NO NATURAL KEY. See notes: every candidate leaves thousands of duplicates, and
# 8,100 rows are identical across all 21 columns. A report extract, not a
# transaction log.
#
# Carries both LOCATION_NAME1 and LOCATION_NAME. The previous parser handled
# this with a hand-written `or` fallback for this one case; they are two
# distinct columns and are mapped as such.""",
        variants=[
            dict(
                name="erx_21",
                require_all=["IMREMED_CODE", "ORDERED_DATE", "MED_TITLE"],
                headers=allscripts,
                overrides=ALS_OVER,
            )
        ],
    ),
)

# ------------------------------------------------------- Curve monthly services
SVC_OVER = auto_pii(
    curve_svc,
    {
        "voucher_number": dict(type=TEXT, required=True),
        "service_id": dict(type=TEXT, required=True),
        "clientid": dict(type=TEXT, pii="join_key"),
        "isvoided": dict(type=TEXT),
        "voidsign": dict(type=NUM),
        "fee": dict(type=MONEY, control_total=True),
        "units": dict(type=NUM, control_total=True),
        "workrvu": dict(type=NUM),
    },
)
SVC_OVER = apply_detected_dates(
    SVC_OVER, EHR / "Curve_Monthly Services 01012023-03062026.csv", curve_svc
)

SVC_NOTES = """No natural key. (Voucher_Number, Service_ID) leaves 5,803
duplicate rows across 230,192, and Service_ID alone leaves the same, so the
voucher adds nothing. There are no exact-duplicate rows, so the row hash is a
sound identity; the missing piece is a line-sequence number the export does not
carry. Monthly Payments, from the same practice-management system, does carry
SeqNum - worth asking whether this report can include it too.

Five SSRS artifact columns (Textbox108, Textbox101, Textbox78, Textbox53,
Textbox38) lead the file and are excluded from matching."""

write(
    "ehr.curve.monthly_services.yaml",
    spec_yaml(
        key="ehr.curve.monthly_services",
        name="Curve - Monthly Services",
        vendor="Curve / Allscripts",
        group="3 AllscriptsCurve",
        catalog_names=["Monthly services"],
        filename_patterns=[r"curve.*monthly service"],
        natural_key=[],
        notes=SVC_NOTES,
        header="""# Curve monthly services - billed charges from the practice-management system.
#
# 122 columns, 230,192 rows, 260 MB. The previous pipeline never loaded this
# source at all.
#
# CARRIES SSN, classified prohibited: matched so drift checks still see it, then
# discarded before any value reaches the raw layer, quarantine or a log.
#
# Overlaps Monthly Payments - both carry Voucher_Number, Service_ID, IsVoided,
# VisitLocAbbr, ProcCode and Fee. They appear to be charges and transactions from
# one system, and which is authoritative for revenue is an open question in
# docs/data-findings.md.
#
# NO NATURAL KEY. See notes.""",
        variants=[
            dict(
                name="services_122",
                require_all=["Voucher_Number", "Service_ID", "ProcCode"],
                headers=curve_svc,
                overrides=SVC_OVER,
            )
        ],
    ),
)

# ---------------------------------------------------------- Curve appointments
# Curve writes three different temporal shapes and they are not interchangeable.
US_DATETIME = ["%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"]
APPT_OVER = auto_pii(
    curve_appt,
    {
        # Blank on a small number of appointments, so not required.
        "patient_number": dict(type=TEXT, pii="join_key"),
        "resource_abbr": dict(type=TEXT),
        # Not times despite the names: bare HHMM integers such as 1454, three or
        # four characters, with no date part and no separator. Typing them as
        # dates quarantined every row on the first run of this spec. Kept as
        # text until somebody decides what they mean relative to which day.
        "check_in_time": dict(
            type=TEXT,
            desc="""HHMM with no separator and no date - 1454 for 14:54. Not a
            timestamp, and not resolvable to one without assuming it belongs to
            the appointment date.""",
        ),
        "check_out_time": dict(
            type=TEXT,
            desc="""HHMM with no separator and no date, as Check_In_Time.""",
        ),
        # Empty in every delivery to date, so no format can be detected from
        # values. Typed by hand to match its siblings rather than left as text,
        # because the day it carries a value it should parse rather than land
        # as a string nobody notices.
        "datetime_bumped": dict(
            type=DATE,
            formats=US_DATETIME,
            desc="""Unpopulated in all deliveries so far. Typed to match the
            other DateTime_ columns on the reasonable assumption Curve writes
            them alike; if a value ever fails to parse the row quarantines,
            which is the outcome that gets it looked at.""",
        ),
        "cm_date_of_injury": dict(
            type=DATE,
            formats=US_DATE,
            desc="Unpopulated in all deliveries so far; typed to match its siblings.",
        ),
    },
)
APPT_OVER = apply_detected_dates(
    APPT_OVER, EHR / "Curve_Appointment Details by Date 01012023-03062026.csv", curve_appt
)

APPT_NOTES = """Three temporal shapes, none interchangeable: plain dates
(Patient_DOB, Date_Of_Account) as MM/DD/YYYY, timestamps (Appointment_DateTime,
DateTime_Scheduled) as MM/DD/YYYY H:MM with a non-padded hour, and
Check_In_Time / Check_Out_Time as bare HHMM integers - 1454 for 14:54 - with no
date part at all. The last of those are kept as text, because resolving them to
a timestamp means assuming which day they belong to.

No natural key. The export carries no appointment identifier at
all; (Patient_Number, Appointment_DateTime) leaves 11,385 duplicate rows across
199,591. Ten rows are exact duplicates across all 101 columns.

Curve holds an appointment id internally - Monthly Services and Monthly Payments
both reference vouchers and services by id - so this is a reporting omission
rather than a modelling one, and worth requesting."""

write(
    "ehr.curve.appointments.yaml",
    spec_yaml(
        key="ehr.curve.appointments",
        name="Curve - Appointment Details by Date",
        vendor="Curve / Allscripts",
        group="3 AllscriptsCurve",
        catalog_names=["Encounter"],
        filename_patterns=[r"curve.*appointment"],
        natural_key=[],
        notes=APPT_NOTES,
        header="""# Curve appointment details.
#
# 101 columns, 199,591 rows, 143 MB. Never loaded by the previous pipeline.
#
# NO NATURAL KEY - the export carries no appointment identifier. See notes.
#
# Heavily identifying: patient and guarantor names, addresses, dates of birth,
# telephone numbers and email. Classified so they land encrypted.""",
        variants=[
            dict(
                name="appointments_101",
                require_all=["Patient_Number", "ResDesc", "Resource_Abbr"],
                headers=curve_appt,
                overrides=APPT_OVER,
            )
        ],
    ),
)

# ---------------------------------------------------------------- Athena x3
ATH_READ = {"preamble_lines": 1}

ATH_ENC_OVER = auto_pii(
    ath_enc,
    {
        "patientid": dict(type=TEXT, required=True, pii="join_key"),
        "cln_enc_date": dict(type=DATE, required=True, formats=US_DATE),
        "patientdob": dict(type=DATE, formats=US_DATE, pii="direct"),
        "prvdr_npi_no": dict(type=TEXT),
        "order_dprtmnt": dict(type=TEXT),
    },
)
write(
    "ehr.athena.encounters.yaml",
    spec_yaml(
        key="ehr.athena.encounters",
        name="Athena - Encounter Report",
        vendor="Athena",
        group="5 Athena",
        catalog_names=["Encounter Report"],
        coverage_start="2024-01-01",
        filename_patterns=[r"athena.*encounter"],
        natural_key=[],
        notes="""No natural key. (patientid, cln enc date) leaves 13,729
duplicate rows across 18,574; adding order dprtmnt changes nothing, and adding
sendto provdr still leaves 1,092. Only all six populated columns together are
unique, which is the row hash by another name.

There are no exact-duplicate rows, so the row hash is a sound identity. Athena
holds an encounter id - the Prescription report exposes it as `cln enc id` - so
adding it here would give this report a real key.""",
        header="""# Athena encounter report.
#
# Header is on row 1: every Athena export opens with a "REPORT NAME :" title.
#
# Column names are truncated abbreviations - cln enc date, prvdrlstnme, sendto
# provdr - which is the vendor's own convention, not corruption.
#
# NO NATURAL KEY, though Athena has one: the Prescription report exposes the
# encounter id as `cln enc id` and this report does not carry it. See notes.
#
# No 2023 data exists for this source.""",
        variants=[
            dict(
                name="encounters_11",
                require_all=["cln enc date", "patientid", "cln enc status"],
                read=ATH_READ,
                headers=ath_enc,
                overrides=ATH_ENC_OVER,
            )
        ],
    ),
)

ATH_RX_OVER = auto_pii(
    ath_rx,
    {
        "patientid": dict(type=TEXT, required=True, pii="join_key"),
        "order_id": dict(type=TEXT, required=True),
        "cln_enc_id": dict(type=TEXT, required=True),
        "order_chartdate": dict(type=DATE, required=True, formats=US_DATE),
        "patientdob": dict(type=DATE, formats=US_DATE, pii="direct"),
        "no_of_refills": dict(type=TEXT),
        "prvdr_npi_no": dict(type=TEXT),
    },
)
write(
    "ehr.athena.prescriptions.yaml",
    spec_yaml(
        key="ehr.athena.prescriptions",
        name="Athena - Prescription Report",
        vendor="Athena",
        group="5 Athena",
        catalog_names=["Prescription Report"],
        coverage_start="2024-01-01",
        filename_patterns=[r"athena.*prescription"],
        natural_key=[],
        notes="""No natural key, and the reason is structural. One prescription
order appears once per ICD-10 diagnosis linked to it: order 1861 on encounter
432 arrives twice, identical except for Z113 against Z206. So order id alone
leaves 24,763 duplicate rows across 28,018, and (cln enc id, order id) leaves
5,930 - the figure the earlier validation pack reported.

Adding the diagnosis code gets to 74 residual duplicates of 28,018, which is
close but not a key. 47 rows are exact duplicates across all 20 columns.

The grain of this report is order-by-diagnosis, not order. Reconciling it at
order level requires collapsing diagnoses first, or a report at one row per
order.""",
        header="""# Athena prescription report.
#
# Header is on row 1: a "REPORT NAME :" title precedes it.
#
# NO NATURAL KEY, structurally: one order appears once per linked ICD-10
# diagnosis, so the grain is order-by-diagnosis rather than order. See notes.
#
# No 2023 data exists for this source.""",
        variants=[
            dict(
                name="prescriptions_20",
                require_all=["order chartdate", "order id", "cln enc id"],
                read=ATH_READ,
                headers=ath_rx,
                overrides=ATH_RX_OVER,
            )
        ],
    ),
)

ATH_ELIG_OVER = auto_pii(
    ath_elig,
    {
        "patientid": dict(type=TEXT, required=True, pii="join_key"),
        "patientdob": dict(type=DATE, formats=US_DATE, pii="direct"),
        "ptnt_dt_f_nwst_qlfyng_vnt": dict(type=DATE, formats=US_DATE),
        "ptnt_dt_f_ldst_qlfyng_vnt": dict(type=DATE, formats=US_DATE),
        "reg_dprtmnt": dict(type=TEXT),
        "curr_dprtmnt": dict(type=TEXT),
    },
)
write(
    "ehr.athena.eligibility.yaml",
    spec_yaml(
        key="ehr.athena.eligibility",
        name="Athena - Patient Eligibility (QE Validation)",
        vendor="Athena",
        group="5 Athena",
        catalog_names=["Patient Eligibility report - QE Validation"],
        filename_patterns=[r"athena.*eligibility"],
        natural_key=["patientid"],
        cadence=None,
        header="""# Athena patient eligibility - the 340B qualifying-event validation report.
#
# A snapshot as of a date, not a period extract - the filename carries "as of"
# rather than a range, and each delivery restates every patient's current
# qualifying event.
#
# Natural key: patientid, unique across all 23,188 rows. The only Athena report
# with a real key.
#
# The qualifying-event dates are the evidence that a dispense is 340B-eligible,
# which makes this the most compliance-relevant of the three Athena reports.""",
        variants=[
            dict(
                name="eligibility_10",
                require_all=["patientid", "reg dprtmnt", "curr dprtmnt"],
                read=ATH_READ,
                headers=ath_elig,
                overrides=ATH_ELIG_OVER,
            )
        ],
    ),
)
