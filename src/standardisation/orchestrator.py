"""
Standardisation Orchestrator (ties together FR-2.1 through FR-2.6)
---------------------------------------------------------------------
Job: take ONE ingested raw record (one JSON file's worth of data) and produce
a LIST of clean, flat rows -- one row per (test result) for lab reports, or
one row per (medicine line) for discharge summaries. Each row's keys map
directly onto columns in the target database schema
(see docs/Ourput-table-ideal-schema.csv).

Why "one row per test" instead of one row per file?
The provided ideal schema (Ourput-table-ideal-schema.csv) is a long/normalised
table -- patient + document metadata is repeated on every row, and each row
holds exactly one test result (or one medicine line). This is the same pattern
relational databases use for one-to-many data (one patient document -> many
tests), and it's what lets the operational UI later filter/aggregate by test
name across all clinics easily.
"""

import json
import logging
import uuid
from pathlib import Path

from ingestion.ingest import extract_response_details, extract_document_metadata
from standardisation.test_name_normaliser import TestNameNormaliser
from standardisation.numeric_unit_normaliser import (
    UnitNormaliser, parse_result_value, parse_range,
)
from standardisation.demographic_normaliser import (
    normalise_gender, normalise_age, normalise_date,
)

logger = logging.getLogger("standardisation.orchestrator")

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


def _normalise_medicine_key(raw_name: str) -> str:
    """
    Normalises a medicine name for dictionary lookup: uppercase, strip
    periods (e.g. 'TAB.' vs 'TAB'), and collapse repeated whitespace.

    Found while testing: 'TAB. DOLO 650' (with a period, as written by one
    clinic) failed to match the dictionary entry 'TAB DOLO 650' (no period,
    as written by another), purely because the lookup was a raw uppercase
    string comparison. Applying this normalisation to BOTH the dictionary
    keys at load time and the incoming medicine name at lookup time fixes
    this whole class of punctuation-only mismatches, instead of just adding
    one more hardcoded variant per clinic.
    """
    if not raw_name:
        return ""
    cleaned = raw_name.upper().replace(".", " ")
    return " ".join(cleaned.split())


def _age_to_storable_string(age_dict):
    """
    normalise_age() returns a structured dict ({"years": .., "months": .., ...})
    which is useful for analysis but doesn't fit a flat TEXT column in the
    canonical schema. This flattens it back to a simple display string
    (e.g. "29Y0M0D") for storage, while the structured parsing logic in
    demographic_normaliser.py remains available for any future analysis
    that wants the broken-out years/months/days.
    """
    if not age_dict:
        return None
    years = age_dict.get("years")
    months = age_dict.get("months")
    days = age_dict.get("days")
    if years is None and months is None and days is None:
        return age_dict.get("original")
    return f"{years or 0}Y{months or 0}M{days or 0}D"


class Standardiser:
    def __init__(self):
        self.test_name_normaliser = TestNameNormaliser(
            str(CONFIG_DIR / "test_name_mapping.json")
        )
        self.unit_normaliser = UnitNormaliser(str(CONFIG_DIR / "unit_mapping.json"))
        with open(CONFIG_DIR / "medicine_mapping.json", "r", encoding="utf-8") as f:
            self.medicine_mapping = {
                _normalise_medicine_key(k): v
                for k, v in json.load(f).items()
                if not k.startswith("_")
            }

    def standardise_record(self, ingested_record: dict) -> list:
        """
        Main entry point. Takes one ingested record (from ingestion.ingest.read_json_files)
        and returns a list of standardised row-dicts ready for validation + DB load.
        """
        rows = []

        if ingested_record.get("parse_error"):
            # File failed to even parse as JSON -- produce one error row so it's
            # still visible in the dead-letter / error log (FR-4.2).
            rows.append({
                "id": str(uuid.uuid4()),
                "document_id": None,
                "record_type": "PARSE_ERROR",
                "file_gcs_path": ingested_record["source_file"],
                "ingested_at": ingested_record["ingested_at"],
                "processing_error": ingested_record["parse_error"],
            })
            return rows

        doc_meta = extract_document_metadata(ingested_record)
        response_details = extract_response_details(ingested_record)

        if not response_details:
            # Valid JSON, but no lab/discharge content found -- not an error,
            # just nothing to standardise (FR-1.3: tolerate varying structures).
            return rows

        for detail in response_details:
            classifier = detail.get("classifier")
            data = detail.get("data", {}) or {}

            if classifier == "lab_report":
                rows.extend(self._standardise_lab_report(
                    data, doc_meta, ingested_record
                ))
            elif classifier == "discharge_summary":
                rows.extend(self._standardise_discharge_summary(
                    data, doc_meta, ingested_record
                ))
            else:
                logger.warning(f"Unknown classifier '{classifier}' -- skipping block.")

        return rows

    # ------------------------------------------------------------------
    # LAB REPORT rows
    # ------------------------------------------------------------------
    def _standardise_lab_report(self, data: dict, doc_meta: dict, ingested_record: dict) -> list:
        rows = []
        basic_info = data.get("basic_info", {}) or {}

        shared_fields = {
            "document_id": doc_meta["document_id"],
            "trace_id": doc_meta["trace_id"],
            "correlation_id": doc_meta["correlation_id"],
            "source_system": doc_meta["source_system"],
            "claim_no": doc_meta["claim_no"],
            "nt_code": doc_meta["nt_code"],
            "consumer_client_id": doc_meta["consumer_client_id"],
            "destination_identifier": doc_meta["destination_identifier"],
            "file_gcs_path": ingested_record["source_file"],
            "ingested_at": ingested_record["ingested_at"],
            "uhid": basic_info.get("uhid"),
            "patient_name": basic_info.get("patient_name"),
            "age": _age_to_storable_string(normalise_age(basic_info.get("age"))),
            "gender": normalise_gender(basic_info.get("gender")),
            "lab_or_hospital_name": basic_info.get("lab_or_hospital_name"),
            "bill_date": normalise_date(basic_info.get("bill_date")),
            "reports_date": normalise_date(basic_info.get("reports_date")),
        }

        report_details = data.get("report_details", []) or []
        if not report_details:
            return rows

        for test_row in report_details:
            rows.append(self._build_test_row(test_row, shared_fields))

        return rows

    def _build_test_row(self, test_row: dict, shared_fields: dict) -> dict:
        raw_test_name = test_row.get("test_name")
        raw_result = test_row.get("result")
        raw_unit = test_row.get("unit")
        raw_range = test_row.get("range")
        raw_analytics = test_row.get("test_analytics")
        page_no = test_row.get("page_no")

        name_result = self.test_name_normaliser.normalise(raw_test_name)
        parsed_result = parse_result_value(raw_result)
        parsed_range = parse_range(raw_range)

        canonical_unit, converted_value = self.unit_normaliser.canonicalise(
            raw_unit, parsed_result["result_value"]
        )

        row = dict(shared_fields)  # copy shared patient/document fields
        row.update({
            "id": str(uuid.uuid4()),
            "record_type": "LAB_TEST_RESULT",
            "page_number": page_no,
            "test_name_original": raw_test_name,
            "test_name_canonical": name_result["canonical_name"] or raw_test_name,
            "normalization_method": name_result["method"],
            "normalization_confidence": name_result["confidence"],
            "result_value": converted_value,
            "result_text": parsed_result["result_text"],
            "value_type": parsed_result["value_type"],
            "unit_original": raw_unit,
            "unit_canonical": canonical_unit,
            "range_low": parsed_range["range_low"],
            "range_high": parsed_range["range_high"],
            "range_text": parsed_range["range_text"],
            "test_analytics": raw_analytics,  # validation module overwrites this
        })
        return row

    # ------------------------------------------------------------------
    # DISCHARGE SUMMARY rows
    # ------------------------------------------------------------------
    def _standardise_discharge_summary(self, data: dict, doc_meta: dict, ingested_record: dict) -> list:
        rows = []

        shared_fields = {
            "document_id": doc_meta["document_id"],
            "trace_id": doc_meta["trace_id"],
            "correlation_id": doc_meta["correlation_id"],
            "source_system": doc_meta["source_system"],
            "claim_no": doc_meta["claim_no"],
            "nt_code": doc_meta["nt_code"],
            "consumer_client_id": doc_meta["consumer_client_id"],
            "destination_identifier": doc_meta["destination_identifier"],
            "file_gcs_path": ingested_record["source_file"],
            "ingested_at": ingested_record["ingested_at"],
            "patient_name": data.get("patientName"),
            "age": _age_to_storable_string(normalise_age(data.get("age"))),
            "gender": normalise_gender(data.get("gender")),
            "doctor_name": data.get("doctorName"),
            "hospital_name": data.get("hospitalName"),
            "hospital_address": data.get("hospitalAddress"),
            "admission_date": normalise_date(data.get("admissionDate")),
            "discharge_date": normalise_date(data.get("dischargeDate")),
            "diagnosis": data.get("diagnosis"),
            "brief_history": data.get("briefHistory"),
            "general_examinations": data.get("generalExaminations"),
            "recommendations": data.get("recommendations"),
            "ward": data.get("ward"),
            "post_discharge_advice": data.get("postDischargeAdvice"),
        }

        medications = data.get("dischargeMedications", []) or []
        if not medications:
            # Still produce one row for the discharge summary itself, with no
            # medicine line, so the encounter isn't silently dropped.
            row = dict(shared_fields)
            row.update({"id": str(uuid.uuid4()), "record_type": "DISCHARGE_SUMMARY"})
            rows.append(row)
            return rows

        for med in medications:
            row = dict(shared_fields)
            raw_medicine = med.get("medicine")
            generic = self.medicine_mapping.get(_normalise_medicine_key(raw_medicine))
            row.update({
                "id": str(uuid.uuid4()),
                "record_type": "DISCHARGE_MEDICATION",
                "medicine": generic or raw_medicine,
                "medicine_original": raw_medicine,
                "dose": med.get("dose"),
                "frequency": med.get("frequency"),
                "medicine_type": med.get("type"),
            })
            rows.append(row)

        return rows
