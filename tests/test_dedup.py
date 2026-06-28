"""
Unit tests for duplicate detection (FR-1.2) and its interaction with
idempotent re-runs (NFR-3.2).

Run with:
    cd src && python -m pytest ../tests/test_dedup.py -v
"""

import sys
import tempfile
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ingestion.dedup import DuplicateDetector


def make_temp_detector():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return DuplicateDetector(path), path


def test_first_time_hash_is_not_a_duplicate():
    detector, path = make_temp_detector()
    try:
        assert detector.is_duplicate("hash-abc") is False
    finally:
        detector.close()
        os.remove(path)


def test_same_hash_different_filename_is_a_duplicate():
    """Mirrors Sample_JSON_file1.json / Sample_JSON_file3.json: same content hash, different filenames."""
    detector, path = make_temp_detector()
    try:
        detector.mark_seen("hash-abc", "file1.json")
        assert detector.is_duplicate("hash-abc") is True
    finally:
        detector.close()
        os.remove(path)


def test_genuine_duplicate_vs_idempotent_rerun_distinction():
    """
    Regression test: re-running the pipeline on the SAME filename should not
    be counted as a new 'genuine duplicate' event (it's idempotency working
    correctly), but a NEW filename with content matching an already-seen
    hash should be counted as a genuine duplicate -- and this must hold true
    even for a filename (like Sample_JSON_file3.json) that is ALWAYS caught
    as a duplicate and therefore never appears in seen_file_hashes.
    """
    detector, path = make_temp_detector()
    try:
        # Pass 1: file1.json processed normally, file3.json is a genuine
        # duplicate of it (same content hash, never-before-seen filename).
        assert detector.was_filename_already_processed("file1.json") is False
        detector.mark_seen("hash-abc", "file1.json")
        detector.mark_filename_seen("file1.json")

        assert detector.was_filename_already_processed("file3.json") is False
        detector.mark_filename_seen("file3.json")  # caught as duplicate, but now "seen"

        # Pass 2: re-running both files again. Neither should be genuine now.
        assert detector.was_filename_already_processed("file1.json") is True
        assert detector.was_filename_already_processed("file3.json") is True
    finally:
        detector.close()
        os.remove(path)


def test_record_duplicate_persists_genuine_flag():
    detector, path = make_temp_detector()
    try:
        detector.mark_seen("hash-abc", "file1.json")
        detector.record_duplicate("hash-abc", "file3.json", is_genuine_duplicate=True)
        detector.record_duplicate("hash-abc", "file1.json", is_genuine_duplicate=False)

        genuine_count = detector.conn.execute(
            "SELECT COUNT(*) FROM duplicate_log WHERE is_genuine_duplicate = 1"
        ).fetchone()[0]
        rerun_count = detector.conn.execute(
            "SELECT COUNT(*) FROM duplicate_log WHERE is_genuine_duplicate = 0"
        ).fetchone()[0]

        assert genuine_count == 1
        assert rerun_count == 1
    finally:
        detector.close()
        os.remove(path)
