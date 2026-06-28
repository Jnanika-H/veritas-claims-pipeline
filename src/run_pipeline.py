"""
Pipeline Runner
----------------
This is the script that actually RUNS the whole pipeline end-to-end:

    INGEST (read JSON files)
        -> STANDARDISE (clean names, numbers, units, demographics)
            -> VALIDATE (range check, outlier detection, analytics flags)
                -> LOAD (write to SQLite, idempotent)

Run it from the project root:
    python src/run_pipeline.py --input sample-data --db veritas.db

This single script is what the Operational UI reads its stats from, and what
you'd schedule (e.g. via cron, Cloud Scheduler, or a Dataflow/Cloud Run job
in production) to run on a fixed interval or be triggered on file arrival.
"""

import argparse
import logging
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ingestion.ingest import read_json_files
from ingestion.dedup import DuplicateDetector
from standardisation.orchestrator import Standardiser
from validation.validator import Validator
from storage.db_loader import DatabaseLoader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pipeline")


def run_pipeline(input_folder: str = None, db_path: str = "veritas.db", single_file: str = None) -> dict:
    standardiser = Standardiser()
    validator = Validator()
    loader = DatabaseLoader(db_path)
    dedup = DuplicateDetector(db_path)

    stats = {
        "run_started_at": datetime.now(timezone.utc).isoformat(),
        "files_seen": 0,
        "files_parsed_ok": 0,
        "files_failed": 0,
        "files_duplicate": 0,
        "rows_loaded": 0,
        "test_results_flagged": 0,
    }

    for ingested_record in read_json_files(folder_path=input_folder, single_file_path=single_file):
        stats["files_seen"] += 1

        if ingested_record.get("parse_error"):
            stats["files_failed"] += 1
            logger.error(f"File failed to parse: {ingested_record['source_file']}")
            rows = standardiser.standardise_record(ingested_record)
            loader.load_rows(rows)
            continue

        stats["files_parsed_ok"] += 1

        # FR-1.2: duplicate detection -- same clinical CONTENT submitted twice,
        # even if it arrives with a different document_id/traceId/claim_no
        # (see sample file1.json / file3.json: identical clinical content,
        # different envelope ids -- a whole-file hash would miss this).
        content_hash = ingested_record.get("clinical_content_hash")
        source_file = ingested_record["source_file"]

        # Check filename history BEFORE marking it seen for this pass.
        filename_seen_before = dedup.was_filename_already_processed(source_file)

        if dedup.is_duplicate(content_hash):
            stats["files_duplicate"] += 1
            # Genuine duplicate = this filename has NEVER been encountered
            # before (in any prior run), but its content matches a different
            # file already processed. If we HAVE seen this exact filename
            # before, it's just an idempotent re-run, not a new duplicate.
            is_genuine = not filename_seen_before
            dedup.record_duplicate(content_hash, source_file, is_genuine)
            dedup.mark_filename_seen(source_file)
            logger.warning(
                f"{source_file}: DUPLICATE clinical content "
                f"detected (matches a previously processed file) -- skipping load."
            )
            continue
        dedup.mark_seen(content_hash, source_file)
        dedup.mark_filename_seen(source_file)

        rows = standardiser.standardise_record(ingested_record)
        rows = validator.validate_batch(rows)

        flagged = [
            r for r in rows
            if r.get("test_analytics") in ("OUTLIER", "ABOVE RANGE", "BELOW RANGE", "INVALID")
        ]
        stats["test_results_flagged"] += len(flagged)

        loaded_count = loader.load_rows(rows)
        stats["rows_loaded"] += loaded_count

        logger.info(
            f"{ingested_record['source_file']}: produced {len(rows)} row(s), "
            f"{len(flagged)} flagged."
        )

    loader.close()
    dedup.close()
    stats["run_finished_at"] = datetime.now(timezone.utc).isoformat()
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Veritas Claims data standardisation pipeline.")
    parser.add_argument("--input", default=None, help="Folder containing input JSON files (processes ALL files in it)")
    parser.add_argument("--file", default=None, help="Path to ONE single JSON file to process instead of a folder")
    parser.add_argument("--db", default="veritas.db", help="Path to SQLite database file")
    args = parser.parse_args()

    if not args.input and not args.file:
        args.input = "sample-data"  # default behaviour unchanged if neither is given

    if args.input and args.file:
        parser.error("Provide either --input (a folder) or --file (one file), not both.")

    result_stats = run_pipeline(input_folder=args.input, db_path=args.db, single_file=args.file)

    print("\n--- PIPELINE RUN SUMMARY ---")
    for key, value in result_stats.items():
        print(f"{key}: {value}")
