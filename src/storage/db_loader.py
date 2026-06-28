"""
Database Loader (FR-4.1, FR-4.2, FR-4.3, NFR-3.2 Idempotency)
-----------------------------------------------------------------
Job: create a SQLite table that matches the client-provided ideal schema
column-for-column (docs/Ourput-table-ideal-schema.csv), and load standardised+
validated rows into it WITHOUT creating duplicates on re-run.

Idempotency strategy (NFR-3.2):
Each row gets a deterministic 'id' derived from a hash of
(document_id, file_gcs_path, record_type, test_name_original, page_number,
medicine, dose) -- NOT a random UUID. This means re-running the pipeline on
the exact same input files produces the exact same row ids, so we can safely
INSERT OR REPLACE: re-processing overwrites the same logical row instead of
duplicating it.

Note on the schema (see Assumptions doc, Technical Assumptions):
The provided schema contains some overlapping/duplicate-looking columns
(e.g. both 'medicine' and 'medication_medicine'; both 'page_no' and
'page_number' and 'report_details_page_no'). Rather than silently dropping
columns the client provided, we keep all 78 columns and populate the
primary/clean versions (e.g. 'medicine', 'page_number') from our pipeline,
leaving the legacy-looking duplicate columns NULL. This is called out
explicitly as an assumption rather than guessed at silently.
"""

import sqlite3
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger("storage.db_loader")

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "docs" / "Ourput-table-ideal-schema.csv"
TABLE_NAME = "standardised_records"


def _load_schema_columns():
    import csv
    columns = []
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            col_name = row["column_name"]
            data_type = row["data_type"]
            sqlite_type = {
                "STRING": "TEXT",
                "FLOAT64": "REAL",
                "TIMESTAMP": "TEXT",
            }.get(data_type, "TEXT")
            columns.append((col_name, sqlite_type))
    return columns


SCHEMA_COLUMNS = _load_schema_columns()
COLUMN_NAMES = [c[0] for c in SCHEMA_COLUMNS]


def compute_row_id(row: dict) -> str:
    """
    Deterministic id so re-running the pipeline on the same input updates the
    same row instead of inserting a duplicate (NFR-3.2 Idempotency).
    """
    key_parts = [
        str(row.get("document_id") or ""),
        str(row.get("file_gcs_path") or ""),
        str(row.get("record_type") or ""),
        str(row.get("test_name_original") or ""),
        str(row.get("page_number") or ""),
        str(row.get("medicine_original") or ""),
        str(row.get("dose") or ""),
    ]
    key = "|".join(key_parts)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class DatabaseLoader:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_table()

    def _create_table(self):
        col_defs = ", ".join(f'"{name}" {sql_type}' for name, sql_type in SCHEMA_COLUMNS)
        self.conn.execute(
            f'CREATE TABLE IF NOT EXISTS {TABLE_NAME} ({col_defs}, PRIMARY KEY ("id"))'
        )
        # Error log table for FR-4.2 (dead-letter store)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS error_log (
                id TEXT PRIMARY KEY,
                source_file TEXT,
                error_message TEXT,
                logged_at TEXT
            )
        """)
        self.conn.commit()

    def load_rows(self, rows: list):
        """
        Inserts or replaces rows. Re-running on identical input overwrites the
        same rows (same deterministic id) rather than duplicating them.
        """
        loaded = 0
        for row in rows:
            if row.get("record_type") == "PARSE_ERROR":
                self._log_error(row)
                continue

            row = dict(row)  # don't mutate caller's dict
            row["id"] = compute_row_id(row)
            row["processed_at"] = datetime.now(timezone.utc).isoformat()

            # Only keep keys that exist in the schema; fill missing ones with None
            clean_row = {col: row.get(col) for col in COLUMN_NAMES}

            placeholders = ", ".join(f":{col}" for col in COLUMN_NAMES)
            col_list = ", ".join(f'"{col}"' for col in COLUMN_NAMES)
            self.conn.execute(
                f'INSERT OR REPLACE INTO {TABLE_NAME} ({col_list}) VALUES ({placeholders})',
                clean_row,
            )
            loaded += 1

        self.conn.commit()
        return loaded

    def _log_error(self, row: dict):
        error_id = hashlib.sha256(
            f"{row.get('file_gcs_path')}|{row.get('processing_error')}".encode("utf-8")
        ).hexdigest()
        self.conn.execute(
            "INSERT OR REPLACE INTO error_log (id, source_file, error_message, logged_at) "
            "VALUES (?, ?, ?, ?)",
            (
                error_id,
                row.get("file_gcs_path"),
                row.get("processing_error"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
