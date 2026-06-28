"""
Numeric Conversion + Unit Harmonisation (FR-2.3, FR-2.4)
----------------------------------------------------------
Job: take a messy result string and pull a clean number out of it, then make
sure that number is expressed in one consistent unit per test across all clinics.

Examples this needs to survive:
    "120000 cells/cu.mm"  -> value=120000.0, unit="cells/cu.mm"
    "120000"              -> value=120000.0, unit=None (no unit given)
    "NEGATIVE"             -> value=None, treated as text result, not numeric
    "POSITIVE"             -> value=None, treated as text result
    "1.5-4.5"              -> value=None (this is a RANGE, not a result -- bad data)
    "98.6 degree F"        -> value=98.6, unit="degree F"
    "LFT ( SGOT - 38, SGPT -14, ALP - 127)" -> combined multi-value string,
                                                too ambiguous to safely split here;
                                                flagged as "combined_value" for
                                                the validation module to mark INVALID

We deliberately do NOT try to guess our way through every possible combined
string -- per the Assumptions doc, multi-value combined fields are flagged
rather than silently (and riskily) parsed.
"""

import re
import json
import logging

logger = logging.getLogger("standardisation.numeric_unit")

# Words that mean "this isn't a number, it's a qualitative result"
NON_NUMERIC_RESULT_WORDS = {
    "POSITIVE", "NEGATIVE", "NORMAL", "ABNORMAL", "REACTIVE", "NON-REACTIVE",
    "PRESENT", "ABSENT", "NIL", "TRACE", "NA", "N/A", "OTE",
}

# Matches a number (int or decimal, optionally with a leading +/-)
NUMBER_PATTERN = re.compile(r"[-+]?\d+\.?\d*")

# Matches things like "1.5-4.5" or "150000-410000" -- a RANGE, not a single result
RANGE_PATTERN = re.compile(r"^\s*[-+]?\d+\.?\d*\s*-\s*[-+]?\d+\.?\d*\s*$")


class UnitNormaliser:
    def __init__(self, unit_mapping_path: str):
        with open(unit_mapping_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self._unit_lookup = {k: v for k, v in raw.items() if not k.startswith("_")}

    def canonicalise(self, raw_unit: str, raw_value: float):
        """
        Given a raw unit string and a numeric value already in that unit,
        returns (canonical_unit, converted_value).
        If the unit isn't recognised, returns the unit unchanged and value unchanged
        (we don't silently guess at conversion factors we don't have).
        """
        if raw_unit is None or raw_value is None:
            return raw_unit, raw_value

        cleaned = raw_unit.strip()
        entry = self._unit_lookup.get(cleaned)
        if entry is None:
            # try case-insensitive fallback
            for known_unit, known_entry in self._unit_lookup.items():
                if known_unit.lower() == cleaned.lower():
                    entry = known_entry
                    break

        if entry is None:
            logger.info(f"Unknown unit '{raw_unit}' -- left unconverted.")
            return raw_unit, raw_value

        return entry["canonical_unit"], raw_value * entry["factor"]


def parse_result_value(raw_result: str):
    """
    Attempts to extract a clean numeric value + a flag describing what kind
    of value this was, from a raw result string.

    Returns a dict:
        {
            "result_value": float or None,
            "result_text": original string, always preserved,
            "value_type": "numeric" | "qualitative" | "range_only" | "combined_value" | "empty"
        }
    """
    if raw_result is None:
        return {"result_value": None, "result_text": None, "value_type": "empty"}

    text = str(raw_result).strip()
    if text == "":
        return {"result_value": None, "result_text": text, "value_type": "empty"}

    upper = text.upper()

    # Qualitative result (POSITIVE/NEGATIVE/etc.) -- not a number, by design
    if upper in NON_NUMERIC_RESULT_WORDS:
        return {"result_value": None, "result_text": text, "value_type": "qualitative"}

    # A range like "1.5-4.5" appearing in the RESULT field (seen in sample data,
    # e.g. file2's "lac/cmm" row) -- this is malformed/swapped data, flag it
    if RANGE_PATTERN.match(text):
        return {"result_value": None, "result_text": text, "value_type": "range_only"}

    # Detect a combined multi-value string, e.g. "LFT ( SGOT - 38, SGPT -14, ALP - 127)"
    # Heuristic: contains a comma AND more than one number AND letters -- too risky
    # to auto-split into separate test results without knowing the test's structure.
    numbers_found = NUMBER_PATTERN.findall(text)
    if "," in text and len(numbers_found) > 1 and re.search(r"[A-Za-z]{2,}", text):
        return {"result_value": None, "result_text": text, "value_type": "combined_value"}

    # Standard case: pull the first number out of the string
    # e.g. "120000 cells/cu.mm" -> 120000.0   |   "98.6 degree F" -> 98.6
    match = NUMBER_PATTERN.search(text)
    if match:
        try:
            value = float(match.group())
            return {"result_value": value, "result_text": text, "value_type": "numeric"}
        except ValueError:
            pass

    # Nothing numeric found at all, and not in our known qualitative word list
    return {"result_value": None, "result_text": text, "value_type": "qualitative"}


def parse_range(raw_range: str):
    """
    Parses a reference range string into (low, high).
    Handles: "4000-10000", "<50", ">6", "8.0 - 23.0", "= 1 COI - Equivocal" (unparseable -> None, None)

    Returns dict: {"range_low": float|None, "range_high": float|None, "range_text": original}
    """
    if not raw_range or not str(raw_range).strip():
        return {"range_low": None, "range_high": None, "range_text": raw_range}

    text = str(raw_range).strip()

    # "<50" style -- treat as 0 to 50
    less_than = re.match(r"^<\s*([-+]?\d+\.?\d*)$", text)
    if less_than:
        return {"range_low": 0.0, "range_high": float(less_than.group(1)), "range_text": text}

    # ">6" style -- treat as 6 to infinity-ish (we just leave high as None to mean "no upper bound")
    greater_than = re.match(r"^>\s*([-+]?\d+\.?\d*)$", text)
    if greater_than:
        return {"range_low": float(greater_than.group(1)), "range_high": None, "range_text": text}

    # "4000-10000" or "8.0 - 23.0" style
    dash_range = re.match(r"^([-+]?\d+\.?\d*)\s*-\s*([-+]?\d+\.?\d*)$", text)
    if dash_range:
        return {
            "range_low": float(dash_range.group(1)),
            "range_high": float(dash_range.group(2)),
            "range_text": text,
        }

    # Anything else (e.g. "Less than 1:80", "= 1 COI - Equivocal", "OTE") is not
    # a parseable numeric range -- keep the original text for audit, no numbers.
    return {"range_low": None, "range_high": None, "range_text": text}
