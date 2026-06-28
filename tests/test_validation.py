"""
Unit tests for the validation module (FR-3.1 through FR-3.4).

Run with:
    cd src && python -m pytest ../tests/test_validation.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from validation.validator import Validator


def make_lab_row(canonical_name, value_type, result_value=None, unit_canonical=None):
    """Helper to build a minimal LAB_TEST_RESULT row for testing."""
    return {
        "record_type": "LAB_TEST_RESULT",
        "test_name_canonical": canonical_name,
        "value_type": value_type,
        "result_value": result_value,
        "unit_canonical": unit_canonical,
        "normalization_method": "exact",
    }


def test_within_range():
    v = Validator()
    row = make_lab_row("HAEMOGLOBIN", "numeric", 14.0, "g/dL")
    result = v.validate_row(row)
    assert result["test_analytics"] == "WITHIN RANGE"


def test_above_range():
    v = Validator()
    row = make_lab_row("HAEMOGLOBIN", "numeric", 20.0, "g/dL")  # high but plausible
    result = v.validate_row(row)
    assert result["test_analytics"] == "ABOVE RANGE"


def test_below_range():
    v = Validator()
    row = make_lab_row("HAEMOGLOBIN", "numeric", 9.0, "g/dL")  # low but plausible
    result = v.validate_row(row)
    assert result["test_analytics"] == "BELOW RANGE"


def test_outlier_takes_priority_over_below_range():
    """Business case example: haemoglobin of 0.1 is an outlier, not just 'below range'."""
    v = Validator()
    row = make_lab_row("HAEMOGLOBIN", "numeric", 0.1, "g/dL")
    result = v.validate_row(row)
    assert result["test_analytics"] == "OUTLIER"


def test_outlier_high_implausible_value():
    """Business case example: haemoglobin of 999."""
    v = Validator()
    row = make_lab_row("HAEMOGLOBIN", "numeric", 999.0, "g/dL")
    result = v.validate_row(row)
    assert result["test_analytics"] == "OUTLIER"


def test_qualitative_result_not_validated_as_numeric():
    v = Validator()
    row = make_lab_row("HAEMOGLOBIN", "qualitative", None, None)
    result = v.validate_row(row)
    assert result["test_analytics"] == ""


def test_combined_value_flagged_invalid():
    v = Validator()
    row = make_lab_row("HAEMOGLOBIN", "combined_value", None, None)
    result = v.validate_row(row)
    assert result["test_analytics"] == "INVALID"


def test_range_only_value_flagged_invalid():
    v = Validator()
    row = make_lab_row("HAEMOGLOBIN", "range_only", None, None)
    result = v.validate_row(row)
    assert result["test_analytics"] == "INVALID"


def test_unknown_test_with_no_reference_range_flagged_invalid():
    v = Validator()
    row = make_lab_row("SOME_TEST_NOT_IN_REFERENCE_RANGES", "numeric", 5.0, "mg/dL")
    result = v.validate_row(row)
    assert result["test_analytics"] == "INVALID"


def test_unit_mismatch_flagged_invalid_not_silently_compared():
    """
    Regression test for the real bug found in Sample_JSON_file2.json: a test
    name matched 'HAEMOGLOBIN' (correct canonical name) but carried a unit
    ('cells/cu.mm') that doesn't belong to haemoglobin at all -- this means
    the source row's columns were shifted/misaligned. Must be flagged, not
    silently validated against the wrong range.
    """
    v = Validator()
    row = make_lab_row("HAEMOGLOBIN", "numeric", 9700.0, "cells/cu.mm")
    result = v.validate_row(row)
    assert result["test_analytics"] == "INVALID"


def test_empty_value_not_validated():
    v = Validator()
    row = make_lab_row("HAEMOGLOBIN", "empty", None, None)
    result = v.validate_row(row)
    assert result["test_analytics"] == ""


def test_non_lab_row_passes_through_unchanged():
    """Discharge medication rows have nothing numeric to validate."""
    v = Validator()
    row = {"record_type": "DISCHARGE_MEDICATION", "medicine": "PARACETAMOL"}
    result = v.validate_row(row)
    assert "test_analytics" not in result or result.get("test_analytics") is None


def test_validate_batch_processes_all_rows():
    v = Validator()
    rows = [
        make_lab_row("HAEMOGLOBIN", "numeric", 14.0, "g/dL"),
        make_lab_row("HAEMOGLOBIN", "numeric", 0.1, "g/dL"),
    ]
    results = v.validate_batch(rows)
    assert results[0]["test_analytics"] == "WITHIN RANGE"
    assert results[1]["test_analytics"] == "OUTLIER"
