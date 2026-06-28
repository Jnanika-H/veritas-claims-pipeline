"""
Validation Module (FR-3.1 through FR-3.4)
-------------------------------------------
Job: look at each standardised test row and decide what "test_analytics"
label it deserves:

    WITHIN RANGE   -- value falls inside the medically accepted range
    ABOVE RANGE    -- value is higher than the accepted range, but still plausible
    BELOW RANGE    -- value is lower than the accepted range, but still plausible
    OUTLIER        -- value is so far outside accepted bounds it's likely a data
                      error or a medically implausible reading (FR-3.2)
    INVALID        -- the field could not be validated at all: non-numeric where
                      a number was expected, a combined/multi-value string, or
                      a missing reference range (FR-3.4)
    (blank)        -- nothing to validate (e.g. qualitative POSITIVE/NEGATIVE
                      results, which aren't numeric and aren't "wrong")

This intentionally keeps OUTLIER separate from ABOVE/BELOW RANGE per FR-3.2 --
an outlier is not just "abnormal", it's "probably broken data" and deserves
extra attention from the ops review queue (FR-5.3).
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger("validation")

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


class Validator:
    def __init__(self):
        with open(CONFIG_DIR / "reference_ranges.json", "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.reference_ranges = {k: v for k, v in raw.items() if not k.startswith("_")}

    def validate_row(self, row: dict) -> dict:
        """
        Mutates and returns the row with 'test_analytics' set correctly.
        Only applies to LAB_TEST_RESULT rows -- discharge summary / medication
        rows pass through unchanged (they have nothing numeric to validate).
        """
        if row.get("record_type") != "LAB_TEST_RESULT":
            return row

        value_type = row.get("value_type")
        canonical_name = row.get("test_name_canonical")
        value = row.get("result_value")

        # Case 1: qualitative result (POSITIVE/NEGATIVE/etc.) -- not a numeric
        # comparison at all. Not wrong, just nothing to range-check.
        if value_type == "qualitative":
            row["test_analytics"] = ""
            return row

        # Case 2: FR-3.4 -- a combined multi-value string, or a range mistakenly
        # placed in the result field. We can't trust this value -- flag INVALID.
        if value_type in ("combined_value", "range_only"):
            row["test_analytics"] = "INVALID"
            return row

        # Case 3: empty/missing result -- nothing to validate
        if value_type == "empty" or value is None:
            row["test_analytics"] = ""
            return row

        # Case 4: we have a numeric value -- but do we know this test's reference range?
        ref = self.reference_ranges.get(canonical_name)
        if ref is None:
            # Test isn't in our reference range dictionary yet. We can't say
            # whether it's normal or not -- flag INVALID rather than guessing,
            # and this becomes a worklist item for ops to add the missing range.
            row["test_analytics"] = "INVALID"
            return row

        # FR-3.4 (contradictory data): if the row's OWN unit doesn't match the
        # expected canonical unit for this test at all (and isn't a known
        # equivalent unit), the source row is internally inconsistent -- e.g.
        # a test name matched "HAEMOGLOBIN" but the unit is "cells/cu.mm"
        # (a cell-count unit, not a concentration unit). This usually means
        # the source extraction shifted columns/rows. We flag it rather than
        # silently comparing a mismatched value against the wrong range.
        unit_canonical = row.get("unit_canonical")
        expected_unit = ref.get("unit")
        if unit_canonical and expected_unit and unit_canonical != expected_unit:
            row["test_analytics"] = "INVALID"
            row["normalization_method"] = (
                f"{row.get('normalization_method', '')}_unit_mismatch"
            )
            return row

        outlier_low = ref.get("outlier_low")
        outlier_high = ref.get("outlier_high")
        low = ref.get("low")
        high = ref.get("high")

        # FR-3.2: outlier check comes FIRST and takes priority over plain
        # above/below range, since an outlier is a more severe flag.
        if outlier_low is not None and value < outlier_low:
            row["test_analytics"] = "OUTLIER"
        elif outlier_high is not None and value > outlier_high:
            row["test_analytics"] = "OUTLIER"
        elif low is not None and value < low:
            row["test_analytics"] = "BELOW RANGE"
        elif high is not None and value > high:
            row["test_analytics"] = "ABOVE RANGE"
        else:
            row["test_analytics"] = "WITHIN RANGE"

        return row

    def validate_batch(self, rows: list) -> list:
        return [self.validate_row(row) for row in rows]
