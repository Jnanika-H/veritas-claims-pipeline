"""
Test Name Normaliser (FR-2.1)
------------------------------
Job: take a messy test name like "aemoglobin", "Hb", "HAEMOGLOBIN", "tal WBC Count"
and map it to ONE clean canonical name like "HAEMOGLOBIN" or "WHITE BLOOD CELL COUNT".

How it decides:
1. EXACT match (case-insensitive) against config/test_name_mapping.json.
   This handles every variant we already know about (fast, 100% certain).
2. FUZZY match fallback -- if the name isn't in our dictionary at all (e.g. a brand
   new clinic sends a typo we've never seen), we compare it against every known
   canonical name + variant using a similarity score. If the best match is above
   a confidence threshold, we use it; otherwise we mark it as "UNRESOLVED" and
   keep the original name so a human can review it later.

This is exactly what NFR-2.1 (zero-code onboarding) and NFR-4.1 (98% coverage)
are asking for: most new clinics' naming quirks should resolve automatically
via fuzzy matching, and ops only needs to add a dictionary entry for the rare
case that's too garbled to auto-resolve.
"""

import json
import logging
from difflib import SequenceMatcher
from pathlib import Path

logger = logging.getLogger("standardisation.test_name")

FUZZY_MATCH_THRESHOLD = 0.72  # below this similarity score, we don't trust the match


class TestNameNormaliser:
    def __init__(self, mapping_config_path: str):
        self.mapping_config_path = mapping_config_path
        self._exact_lookup = {}      # "HB" -> "HAEMOGLOBIN" (everything uppercased)
        self._canonical_names = []   # ["HAEMOGLOBIN", "WHITE BLOOD CELL COUNT", ...]
        self._non_lab_terms = set()  # vitals/symptoms/headers/junk -- not lab tests at all
        self._load_config()

    def _load_config(self):
        with open(self.mapping_config_path, "r", encoding="utf-8") as f:
            raw_config = json.load(f)

        for key, value in raw_config.items():
            if key == "_non_lab_terms":
                # A flat list of known non-test terms (vitals, symptoms, panel
                # headers, junk) -- these should be recognised and labelled
                # distinctly from genuinely unresolved/unknown test names.
                self._non_lab_terms = {term.upper().strip() for term in value}
                continue
            if key.startswith("_"):
                continue  # skip "_comment" / "_non_lab_terms_comment" keys

            canonical_name = key
            variants = value
            self._canonical_names.append(canonical_name)
            # the canonical name itself is always a valid match for itself
            self._exact_lookup[canonical_name.upper().strip()] = canonical_name
            for variant in variants:
                self._exact_lookup[variant.upper().strip()] = canonical_name

        logger.info(
            f"Loaded {len(self._canonical_names)} canonical test names, "
            f"{len(self._exact_lookup)} total known variants, "
            f"{len(self._non_lab_terms)} known non-lab terms."
        )

    def normalise(self, raw_test_name: str) -> dict:
        """
        Returns:
            {
                "canonical_name": "HAEMOGLOBIN" or None,
                "method": "exact" | "fuzzy" | "non_lab_term" | "unresolved",
                "confidence": 1.0 | 0.0-1.0 | 0.0
            }

        "non_lab_term" means the original text is a KNOWN vital sign, symptom,
        panel/section header, or placeholder junk -- not a lab test at all, and
        not something the dictionary failed to resolve. This is intentionally
        kept separate from "unresolved" (a genuinely unrecognised test name
        that needs dictionary review) so the two aren't confused on the
        dashboard (see docs/ASSUMPTIONS.md).
        """
        if not raw_test_name or not raw_test_name.strip():
            return {"canonical_name": None, "method": "unresolved", "confidence": 0.0}

        cleaned = raw_test_name.upper().strip()

        # 0. Known non-lab term (vital sign, symptom, section header, junk)
        if cleaned in self._non_lab_terms:
            return {"canonical_name": None, "method": "non_lab_term", "confidence": 1.0}

        # 1. Exact match -- fast path, handles known clinics/variants
        if cleaned in self._exact_lookup:
            return {
                "canonical_name": self._exact_lookup[cleaned],
                "method": "exact",
                "confidence": 1.0,
            }

        # 2. Fuzzy match fallback -- handles new/garbled/truncated names we've
        #    never seen before (e.g. OCR cut off the first letter: "aemoglobin")
        best_match, best_score = self._best_fuzzy_match(cleaned)
        if best_score >= FUZZY_MATCH_THRESHOLD:
            logger.info(
                f"Fuzzy-matched '{raw_test_name}' -> '{best_match}' "
                f"(confidence={best_score:.2f})"
            )
            return {
                "canonical_name": best_match,
                "method": "fuzzy",
                "confidence": round(best_score, 2),
            }

        # 3. Nothing close enough -- flag for human review, keep original name
        logger.warning(
            f"UNRESOLVED test name: '{raw_test_name}' "
            f"(best guess was '{best_match}' at {best_score:.2f}, below threshold)"
        )
        return {"canonical_name": None, "method": "unresolved", "confidence": 0.0}

    def _best_fuzzy_match(self, cleaned_name: str):
        """
        Compares cleaned_name against every known variant string using
        Python's built-in SequenceMatcher (no extra dependency needed).
        Returns (best_canonical_name, best_score).
        """
        best_score = 0.0
        best_canonical = None
        for known_variant, canonical in self._exact_lookup.items():
            score = SequenceMatcher(None, cleaned_name, known_variant).ratio()
            if score > best_score:
                best_score = score
                best_canonical = canonical
        return best_canonical, best_score
