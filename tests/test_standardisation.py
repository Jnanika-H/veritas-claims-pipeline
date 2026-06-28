"""
Unit tests for the standardisation module.

Run with:
    cd src && python -m pytest ../tests/test_standardisation.py -v

These cover FR-2.1 through FR-2.5. Examples are taken directly from the
provided sample JSON files plus a few synthetic edge cases (e.g. clean
gender/date values, since the sample data has these redacted).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from standardisation.test_name_normaliser import TestNameNormaliser
from standardisation.numeric_unit_normaliser import (
    UnitNormaliser, parse_result_value, parse_range,
)
from standardisation.demographic_normaliser import (
    normalise_gender, normalise_age, normalise_date,
)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


# ----------------------------------------------------------------------
# FR-2.1: Test Name Normalisation
# ----------------------------------------------------------------------
def test_exact_match_known_canonical_name():
    normaliser = TestNameNormaliser(str(CONFIG_DIR / "test_name_mapping.json"))
    result = normaliser.normalise("HAEMOGLOBIN")
    assert result["canonical_name"] == "HAEMOGLOBIN"
    assert result["method"] == "exact"
    assert result["confidence"] == 1.0


def test_exact_match_known_variant():
    normaliser = TestNameNormaliser(str(CONFIG_DIR / "test_name_mapping.json"))
    result = normaliser.normalise("Hb")
    assert result["canonical_name"] == "HAEMOGLOBIN"
    assert result["method"] == "exact"


def test_exact_match_truncated_name_from_sample_data():
    """'aemoglobin' is the exact garbled name seen in Sample_JSON_file2.json."""
    normaliser = TestNameNormaliser(str(CONFIG_DIR / "test_name_mapping.json"))
    result = normaliser.normalise("aemoglobin")
    assert result["canonical_name"] == "HAEMOGLOBIN"
    assert result["method"] == "exact"


def test_fuzzy_match_unseen_variant():
    """Something close to a known name, but not literally in the dictionary."""
    normaliser = TestNameNormaliser(str(CONFIG_DIR / "test_name_mapping.json"))
    result = normaliser.normalise("Haemglobin")  # one letter dropped, never seen before
    assert result["canonical_name"] == "HAEMOGLOBIN"
    assert result["method"] == "fuzzy"
    assert result["confidence"] >= 0.72


def test_unresolved_for_completely_unknown_name():
    normaliser = TestNameNormaliser(str(CONFIG_DIR / "test_name_mapping.json"))
    result = normaliser.normalise("ZZZZ_NOT_A_REAL_TEST_QQQ")
    assert result["canonical_name"] is None
    assert result["method"] == "unresolved"
    assert result["confidence"] == 0.0


def test_empty_test_name_is_unresolved_not_crash():
    normaliser = TestNameNormaliser(str(CONFIG_DIR / "test_name_mapping.json"))
    result = normaliser.normalise("")
    assert result["canonical_name"] is None
    assert result["method"] == "unresolved"

    result_none = normaliser.normalise(None)
    assert result_none["canonical_name"] is None


def test_known_vital_sign_classified_as_non_lab_term_not_unresolved():
    """
    'BP', 'Temp', 'Pulse' etc. are vitals mixed into lab report rows in the
    sample data (Sample_JSON_file5.json) -- they should be recognised as
    known non-lab terms, distinct from genuinely unresolved test names.
    """
    normaliser = TestNameNormaliser(str(CONFIG_DIR / "test_name_mapping.json"))
    result = normaliser.normalise("BP")
    assert result["canonical_name"] is None
    assert result["method"] == "non_lab_term"


def test_known_junk_placeholder_classified_as_non_lab_term():
    """'test_name' is the literal placeholder/template string seen in Sample_JSON_file5.json."""
    normaliser = TestNameNormaliser(str(CONFIG_DIR / "test_name_mapping.json"))
    result = normaliser.normalise("test_name")
    assert result["method"] == "non_lab_term"


# ----------------------------------------------------------------------
# FR-2.3: Numeric Conversion
# ----------------------------------------------------------------------
def test_numeric_value_with_unit_in_string():
    """'120000 cells/cu.mm' style result, from the business case description."""
    result = parse_result_value("120000 cells/cu.mm")
    assert result["result_value"] == 120000.0
    assert result["value_type"] == "numeric"


def test_plain_numeric_string():
    result = parse_result_value("120000")
    assert result["result_value"] == 120000.0
    assert result["value_type"] == "numeric"


def test_qualitative_result_negative():
    result = parse_result_value("NEGATIVE")
    assert result["result_value"] is None
    assert result["value_type"] == "qualitative"


def test_qualitative_result_positive():
    result = parse_result_value("POSITIVE")
    assert result["result_value"] is None
    assert result["value_type"] == "qualitative"


def test_range_mistakenly_in_result_field():
    """Seen in Sample_JSON_file2.json: result='1.5-4.5' (a range, not a value)."""
    result = parse_result_value("1.5-4.5")
    assert result["result_value"] is None
    assert result["value_type"] == "range_only"


def test_combined_multi_value_string_flagged_not_guessed():
    """Seen in Sample_JSON_file5.json: 'LFT ( SGOT - 38, SGPT -14, ALP - 127)'."""
    result = parse_result_value("LFT ( SGOT - 38, SGPT -14, ALP - 127)")
    assert result["result_value"] is None
    assert result["value_type"] == "combined_value"


def test_empty_result_is_empty_not_error():
    result = parse_result_value("")
    assert result["value_type"] == "empty"
    result_none = parse_result_value(None)
    assert result_none["value_type"] == "empty"


def test_decimal_with_unit():
    result = parse_result_value("98.6 degree F")
    assert result["result_value"] == 98.6


# ----------------------------------------------------------------------
# FR-2.4: Unit Harmonisation
# ----------------------------------------------------------------------
def test_unit_canonicalisation_no_conversion_needed():
    normaliser = UnitNormaliser(str(CONFIG_DIR / "unit_mapping.json"))
    unit, value = normaliser.canonicalise("g/dL", 12.5)
    assert unit == "g/dL"
    assert value == 12.5


def test_unit_canonicalisation_case_variant():
    normaliser = UnitNormaliser(str(CONFIG_DIR / "unit_mapping.json"))
    unit, value = normaliser.canonicalise("mg/dl", 1.2)
    assert unit == "mg/dL"
    assert value == 1.2


def test_unit_canonicalisation_with_scale_conversion():
    """mil/cu.cm -> mil/cu.mm requires a 0.001 factor (business case example)."""
    normaliser = UnitNormaliser(str(CONFIG_DIR / "unit_mapping.json"))
    unit, value = normaliser.canonicalise("mil/cu.cm", 5450.0)
    assert unit == "mil/cu.mm"
    assert value == 5.45


def test_unknown_unit_left_unconverted_not_guessed():
    normaliser = UnitNormaliser(str(CONFIG_DIR / "unit_mapping.json"))
    unit, value = normaliser.canonicalise("some_never_seen_unit", 10.0)
    assert unit == "some_never_seen_unit"
    assert value == 10.0  # unchanged, not silently scaled


# ----------------------------------------------------------------------
# Range Parsing
# ----------------------------------------------------------------------
def test_parse_dash_range():
    result = parse_range("4000-10000")
    assert result["range_low"] == 4000.0
    assert result["range_high"] == 10000.0


def test_parse_less_than_range():
    result = parse_range("<50")
    assert result["range_low"] == 0.0
    assert result["range_high"] == 50.0


def test_parse_greater_than_range():
    result = parse_range(">6")
    assert result["range_low"] == 6.0
    assert result["range_high"] is None


def test_parse_unparseable_range_kept_as_text():
    result = parse_range("Less than 1:80")
    assert result["range_low"] is None
    assert result["range_high"] is None
    assert result["range_text"] == "Less than 1:80"


# ----------------------------------------------------------------------
# FR-2.5: Demographic Normalisation
# ----------------------------------------------------------------------
def test_normalise_gender_variants():
    assert normalise_gender("M") == "MALE"
    assert normalise_gender("Male") == "MALE"
    assert normalise_gender("F") == "FEMALE"
    assert normalise_gender("female") == "FEMALE"


def test_normalise_gender_redacted_placeholder():
    assert normalise_gender("[GENDER REDACTED]") is None


def test_normalise_age_years_months_days():
    result = normalise_age("33Y11M26D")
    assert result["years"] == 33
    assert result["months"] == 11
    assert result["days"] == 26


def test_normalise_age_plain_number():
    result = normalise_age("45")
    assert result["years"] == 45


def test_normalise_age_redacted_placeholder():
    assert normalise_age("[AGE REDACTED]") is None


def test_normalise_date_dd_mm_yyyy():
    assert normalise_date("09-10-2025") == "2025-10-09"


def test_normalise_date_dd_mon_yyyy():
    assert normalise_date("07-Oct-2025") == "2025-10-07"


def test_normalise_date_dd_slash_mon_slash_yyyy():
    assert normalise_date("08/Oct/2025") == "2025-10-08"


def test_normalise_date_placeholder_junk_returns_none():
    """'DD/MM/YYYY' is literal placeholder text seen in Sample_JSON_file5.json."""
    assert normalise_date("DD/MM/YYYY") is None


def test_normalise_date_empty_returns_none():
    assert normalise_date("") is None
    assert normalise_date(None) is None


def test_age_dict_flattens_to_storable_string():
    """
    Regression test: normalise_age() returns a structured dict, which broke
    the SQLite loader when stored directly (sqlite3.ProgrammingError: type
    'dict' is not supported). Found while testing the pipeline on a new
    single-file submission with a real (non-redacted) age value.
    """
    from standardisation.orchestrator import _age_to_storable_string

    age_dict = normalise_age("29Y0M0D")
    flat = _age_to_storable_string(age_dict)
    assert isinstance(flat, str)
    assert flat == "29Y0M0D"

    # Redacted/missing age should flatten to None, not crash
    assert _age_to_storable_string(None) is None


def test_medicine_key_normalisation_handles_period_variants():
    """
    Regression test: 'TAB. DOLO 650' (with a period) failed to match the
    dictionary entry 'TAB DOLO 650' (no period) because the lookup was a
    raw uppercase string comparison. Found while testing a richer sample
    file with varied medicine name punctuation.
    """
    from standardisation.orchestrator import _normalise_medicine_key

    assert _normalise_medicine_key("TAB. DOLO 650") == _normalise_medicine_key("TAB DOLO 650")
    assert _normalise_medicine_key("tab.  dolo   650") == _normalise_medicine_key("TAB DOLO 650")
    assert _normalise_medicine_key(None) == ""
    assert _normalise_medicine_key("") == ""
