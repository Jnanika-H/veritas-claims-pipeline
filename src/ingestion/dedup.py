"""
Duplicate Detection (FR-1.2)
------------------------------
Job: detect when the exact same clinical content has been submitted more than
once -- e.g. the same patient's discharge summary sent twice, possibly from
two different source systems, possibly under two different document_ids
(which is exactly what happens in our sample data: Sample_JSON_file1.json and
Sample_JSON_file3.json are byte-for-byte identical clinical content, but the
source system gave them two different document_ids and trace_ids).

Why we can't just rely on document_id:
A real source system might assign a new document_id to a re-submission, so
matching on document_id alone would MISS this kind of duplicate. Instead we
fingerprint the actual file CONTENT (see ingestion.ingest.compute_file_hash)
and remember every hash we've already processed, persisted in its own SQLite
table so the check still works across separate pipeline runs (not just within
one run) -- this is what NFR-3.2 (idempotency) requires in practice.

Configurable per NFR-2.1: the dedup STRATEGY here is "exact content match".
A production system handling near-duplicates (same patient, slightly
different formatting) would extend this with fuzzy/semantic matching --
documented as a Scope Exclusion in the Assumptions doc.
"""

import sqlite3
import logging
from datetime import datetime, timezone

logger = logging.getLogger("ingestion.dedup")


class DuplicateDetector:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self._create_table()

    def _create_table(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_file_hashes (
                file_hash TEXT PRIMARY KEY,
                first_seen_file TEXT,
                first_seen_at TEXT
            )
        """)
        # Tracks every time a DUPLICATE was caught (i.e. a hash that was
        # already in seen_file_hashes). This is separate from
        # seen_file_hashes, which only ever holds one row per unique file --
        # this table is what the dashboard's "Duplicate Files Caught" count
        # should read from.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS duplicate_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_hash TEXT,
                duplicate_source_file TEXT,
                detected_at TEXT,
                is_genuine_duplicate INTEGER DEFAULT 1
            )
        """)
        # Tracks every FILENAME we have ever encountered, regardless of
        # whether it was loaded or caught as a duplicate. This is what makes
        # it possible to tell "this filename has been seen before" (so a
        # repeat run is just idempotency) apart from "this is a brand new
        # filename whose content happens to match something else" (a
        # genuine duplicate, FR-1.2) -- including for filenames like
        # Sample_JSON_file3.json that are ALWAYS caught as duplicates and
        # therefore never appear in seen_file_hashes.first_seen_file.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_filenames (
                source_file TEXT PRIMARY KEY,
                first_seen_at TEXT
            )
        """)
        self.conn.commit()

    def record_duplicate(self, file_hash: str, source_file: str, is_genuine_duplicate: bool):
        """
        Logs one occurrence of a duplicate being caught (FR-1.2 visibility).

        is_genuine_duplicate distinguishes two different situations that both
        cause is_duplicate() to return True, but mean very different things:
        - True:  this exact FILENAME has never been encountered before (in
                 any prior run), but its CONTENT matches a different file we
                 already processed (a real business duplicate -- e.g. file3
                 matching file1).
        - False: this exact filename has been encountered before (whether it
                 was loaded, or itself caught as a duplicate, in a previous
                 run) -- we're just re-running the same batch again. This is
                 idempotency working correctly, not a new duplicate event.
        """
        self.conn.execute(
            "INSERT INTO duplicate_log (file_hash, duplicate_source_file, detected_at, is_genuine_duplicate) "
            "VALUES (?, ?, ?, ?)",
            (file_hash, source_file, datetime.now(timezone.utc).isoformat(), int(is_genuine_duplicate)),
        )
        self.conn.commit()

    def is_duplicate(self, file_hash: str) -> bool:
        if file_hash is None:
            return False
        cursor = self.conn.execute(
            "SELECT 1 FROM seen_file_hashes WHERE file_hash = ?", (file_hash,)
        )
        return cursor.fetchone() is not None

    def was_filename_already_processed(self, source_file: str) -> bool:
        """
        True if this exact filename has appeared in any previous run --
        whether it was loaded as a new file, or itself caught as a duplicate.
        Call mark_filename_seen() for every file (loaded or duplicate) so
        this stays accurate across repeated runs.
        """
        cursor = self.conn.execute(
            "SELECT 1 FROM seen_filenames WHERE source_file = ?", (source_file,)
        )
        return cursor.fetchone() is not None

    def mark_filename_seen(self, source_file: str):
        """Records that this filename has now been encountered, regardless of outcome."""
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_filenames (source_file, first_seen_at) VALUES (?, ?)",
            (source_file, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def mark_seen(self, file_hash: str, source_file: str):
        if file_hash is None:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_file_hashes (file_hash, first_seen_file, first_seen_at) "
            "VALUES (?, ?, ?)",
            (file_hash, source_file, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
