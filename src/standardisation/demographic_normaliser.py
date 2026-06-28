"""
Demographic Normaliser (FR-2.5 - Optional)
--------------------------------------------
Job: clean up age, gender, and date fields so they're consistent no matter
which clinic sent them.

Note on the sample data: most age/gender/patient-name fields in the sample
files are pre-redacted by the source system (e.g. "[AGE REDACTED]",
"[GENDER REDACTED]") for privacy. This module still needs to exist and work
correctly for the cases where real values DO come through -- which is the
normal case in production, and is testable with synthetic examples (see
tests/test_standardisation.py).
"""

import re
import logging
from datetime import datetime

logger = logging.getLogger("standardisation.demographics")

# Formats we've seen across the sample files: "09-10-2025", "07-Oct-2025",
# "08/Oct/2025", "DD/MM/YYYY" (placeholder/junk -- should fail safely)
DATE_FORMATS_TO_TRY = [
    "%d-%m-%Y",
    "%d-%b-%Y",
    "%d/%b/%Y",
    "%d/%m/%Y",
    "%Y-%m-%d",
]


def normalise_gender(raw_gender: str):
    """
    'M', 'Male', 'male', 'F', 'Female' -> 'MALE' / 'FEMALE' / None if unrecognised.
    Redacted placeholders (e.g. "[GENDER REDACTED]") pass through as None,
    not as an error -- they're intentionally missing, not malformed.
    """
    if not raw_gender:
        return None
    cleaned = raw_gender.strip().upper()
    if cleaned.startswith("[") :  # redacted placeholder
        return None
    if cleaned in ("M", "MALE"):
        return "MALE"
    if cleaned in ("F", "FEMALE"):
        return "FEMALE"
    logger.info(f"Unrecognised gender value: '{raw_gender}' -- left as-is.")
    return raw_gender


def normalise_age(raw_age: str):
    """
    Parses ages like '33Y11M26D' (33 years, 11 months, 26 days) into a
    structured dict, or returns None for redacted/missing values.

    Returns: {"years": int, "months": int, "days": int, "original": str} or None
    """
    if not raw_age:
        return None
    cleaned = raw_age.strip()
    if cleaned.startswith("["):  # redacted placeholder
        return None

    match = re.match(
        r"^(?:(\d+)\s*Y)?(?:(\d+)\s*M)?(?:(\d+)\s*D)?$", cleaned, re.IGNORECASE
    )
    if match and any(match.groups()):
        years, months, days = match.groups()
        return {
            "years": int(years) if years else 0,
            "months": int(months) if months else 0,
            "days": int(days) if days else 0,
            "original": cleaned,
        }

    # Plain number, e.g. "45" -- treat as years
    if cleaned.isdigit():
        return {"years": int(cleaned), "months": 0, "days": 0, "original": cleaned}

    logger.info(f"Unrecognised age format: '{raw_age}' -- left as raw text.")
    return {"years": None, "months": None, "days": None, "original": cleaned}


def normalise_date(raw_date: str):
    """
    Tries each known date format until one works. Returns ISO 8601 ('YYYY-MM-DD')
    string, or None if the value is a placeholder/unparseable (e.g. 'DD/MM/YYYY'
    seen in sample file 5, which is literal placeholder junk, not a real date).
    """
    if not raw_date or not raw_date.strip():
        return None

    cleaned = raw_date.strip()

    # Catch obvious placeholder junk before even trying to parse
    if cleaned.upper() in ("DD/MM/YYYY", "MM/DD/YYYY", "N/A", "NA"):
        return None

    for fmt in DATE_FORMATS_TO_TRY:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    logger.info(f"Could not parse date '{raw_date}' with any known format.")
    return None
