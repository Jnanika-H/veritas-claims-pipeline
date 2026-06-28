"""
Ingestion Module (FR-1)
-----------------------
Job: read raw JSON files from a folder (this simulates reading from a GCS bucket --
in production you'd swap this for a GCS client, but the rest of the pipeline
never needs to know the difference).

What this does:
1. Finds every .json file in the input folder.
2. Tries to parse each one. If a file is broken/corrupt JSON, it does NOT crash
   the whole pipeline -- it logs the error and moves to the next file (FR-1, NFR-3.1).
3. Pulls out the parts of the JSON we actually care about (patient info, document
   id, and the list of "responseDetails" -- which may contain a lab_report,
   a discharge_summary, or both).
4. Computes a fingerprint (hash) of each file's content, used later by the
   standardisation module to detect duplicate submissions (FR-1.2).

This module does NOT clean or standardise anything yet -- it just safely gets
the raw data into Python so later stages can work on it.
"""

import json
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger("ingestion")


class IngestionError(Exception):
    """Raised when a file cannot be ingested at all (unreadable, not JSON)."""
    pass


def compute_file_hash(raw_text: str) -> str:
    """
    A 'fingerprint' for the file's exact content -- used for cheap, exact
    "is this literally the same file" checks where needed elsewhere.
    NOTE: for actual clinical-duplicate detection, use
    compute_clinical_content_hash() instead -- see that function's docstring
    for why hashing the whole file is NOT sufficient for FR-1.2.
    """
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


def compute_clinical_content_hash(parsed_json: dict) -> str:
    """
    Fingerprints only the CLINICAL content of a record (data.responseDetails),
    deliberately excluding envelope/tracking fields like traceId, documentId,
    correlationId, and claim_no.

    Why this matters: in real submissions, the same patient encounter can be
    resubmitted by a different source system with a BRAND NEW documentId,
    traceId, and claim_no, while the actual diagnosis/medications/test results
    are identical. A whole-file hash would treat these as two different files
    and miss the duplicate. By hashing only the clinical payload, we correctly
    catch this case (this is exactly what happens with the sample files
    Sample_JSON_file1.json and Sample_JSON_file3.json).

    Returns None if there's no responseDetails block to fingerprint.
    """
    try:
        response_details = parsed_json["data"]["responseDetails"]
    except (KeyError, TypeError):
        return None

    # Sort keys for a stable, order-independent fingerprint.
    canonical_text = json.dumps(response_details, sort_keys=True)
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


def read_json_files(folder_path: str = None, single_file_path: str = None):
    """
    Generator that yields one parsed record per JSON file.

    Two modes:
    - folder_path: process every .json file found in this folder (default mode,
      simulates a GCS bucket / batch of arriving files).
    - single_file_path: process exactly ONE named JSON file instead of a whole
      folder. Useful for testing a single clinic submission in isolation, or
      for an on-demand "process this one file now" trigger.

    Exactly one of folder_path / single_file_path should be provided.

    Each yielded item is a dict:
        {
            "source_file": "Sample_JSON_file1.json",
            "file_hash": "...",
            "raw": <the full parsed JSON>,
            "ingested_at": "2026-06-27T12:00:00Z"
        }

    Files that fail to parse are logged and skipped -- they don't stop the run.
    """
    if single_file_path:
        json_files = [Path(single_file_path)]
        if not json_files[0].exists():
            raise IngestionError(f"Input file does not exist: {single_file_path}")
    else:
        folder = Path(folder_path)
        if not folder.exists():
            raise IngestionError(f"Input folder does not exist: {folder_path}")
        json_files = sorted(folder.glob("*.json"))
        if not json_files:
            logger.warning(f"No .json files found in {folder_path}")

    for file_path in json_files:
        try:
            raw_text = file_path.read_text(encoding="utf-8")
            parsed = json.loads(raw_text)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            # FR-1 / NFR-3.1: one bad file must not break the pipeline.
            logger.error(f"FAILED to parse {file_path.name}: {e}")
            yield {
                "source_file": file_path.name,
                "file_hash": None,
                "raw": None,
                "ingested_at": datetime.now(timezone.utc).isoformat(),
                "parse_error": str(e),
            }
            continue

        yield {
            "source_file": file_path.name,
            "file_hash": compute_file_hash(raw_text),
            "clinical_content_hash": compute_clinical_content_hash(parsed),
            "raw": parsed,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "parse_error": None,
        }


def extract_response_details(record: dict) -> list:
    """
    Pulls the list of 'responseDetails' entries out of a raw ingested record.
    Each entry has a 'classifier' (e.g. 'lab_report' or 'discharge_summary')
    and a 'data' block with the actual clinical content.

    Returns an empty list (not an error) if the structure is missing --
    this is intentionally forgiving, since FR-1.3 requires us to accept
    varying JSON structures across clinics without crashing.
    """
    raw = record.get("raw")
    if not raw:
        return []
    try:
        return raw["data"]["responseDetails"]
    except (KeyError, TypeError):
        logger.warning(
            f"{record['source_file']}: could not find data.responseDetails -- "
            f"skipping clinical content for this file."
        )
        return []


def extract_document_metadata(record: dict) -> dict:
    """
    Pulls document-level + claim-level metadata that applies to every row
    we eventually produce for this file: document_id, trace_id, correlation_id,
    claim_no, nt_code, source_system, consumer_client_id, destination_identifier.

    These map directly to columns in the target database schema.
    """
    raw = record.get("raw") or {}
    data = raw.get("data", {}) if isinstance(raw, dict) else {}

    meta = {
        "document_id": data.get("documentId"),
        "trace_id": raw.get("traceId"),
        "correlation_id": data.get("correlationId"),
        "source_system": None,
        "claim_no": None,
        "nt_code": None,
        "consumer_client_id": None,
        "destination_identifier": None,
    }

    for item in data.get("metaDetails", []) or []:
        key = item.get("key")
        value = item.get("value")
        if key == "source_system":
            meta["source_system"] = value
        elif key == "claim_no":
            meta["claim_no"] = value
        elif key == "nt_code":
            meta["nt_code"] = value
        elif key == "ConsumerClientId":
            meta["consumer_client_id"] = value
        elif key == "DestinationIdentifier":
            meta["destination_identifier"] = value

    return meta
