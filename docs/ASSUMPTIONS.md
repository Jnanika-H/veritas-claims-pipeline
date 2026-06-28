# Assumptions Document
## Veritas Claims — Medical Data Standardisation Pipeline

This document states explicitly what was assumed, simplified, or
deliberately left out, and why — per the assignment's evaluation note that
clearly stated assumptions score higher than silent guesses.

---

## A. Business Assumptions
*What we assumed about the business context, volume, or process that is
not stated explicitly.*

**B1. "Clinic" identity in the sample data.**
The provided sample JSON files don't carry an explicit clinic ID or clinic
name field — patient name, hospital name, and lab name are all redacted
with placeholders like `[HOSPITAL NAME REDACTED]`. The Operational UI's
Clinic-level Summary (FR-5.4) therefore groups by `file_gcs_path` (source
file) as a stand-in for clinic identity. In production, with a real
`clinic_id` field, this is a one-line change to the UI's `GROUP BY` — no
pipeline logic changes needed.

**B2. One file equals one patient encounter.**
Each JSON file is treated as one discrete submission event, potentially
containing both a `lab_report` and a `discharge_summary` block for the
same encounter (as seen in `Sample_JSON_file2.json`). We assumed files are
not expected to span multiple unrelated patients.

**B3. "Duplicate" means same clinical content, not same file bytes.**
The business case explicitly says duplicates can arrive "from different
systems" — confirmed by the sample data, where `Sample_JSON_file1.json` and
`Sample_JSON_file3.json` have identical diagnosis/medication content but
different `documentId`, `traceId`, and `claim_no` values. We therefore
fingerprint only the clinical payload (`data.responseDetails`), not the
whole file, for duplicate detection. This is a deliberate, configurable
choice (NFR-2.1) — a stricter or looser dedup strategy could be swapped in
via the same `DuplicateDetector` interface.

---

## B. Technical Assumptions
*Infrastructure, database, framework, and tooling choices, and why.*

**T1. Database choice: SQLite for the prototype.**
The assignment explicitly allows "SQLite or PostgreSQL (or BigQuery if
preferred)" for the take-home scope. SQLite was chosen to keep the
submission runnable with zero setup (no server, no credentials) for
reviewers. All row-building/loading code is plain SQL via Python's standard
library — moving to PostgreSQL or BigQuery is a connection-string-level
change, not a redesign.

**T2. The provided output schema is the source of truth for column
names** — even though it differs from the "5 columns per test, pivoted
into named columns" layout described in section 2.1 of the assignment text
(FR-2.2). The actual attached schema (`Ourput-table-ideal-schema.csv`) is a
long/normalised table — one row per test result (or per medicine line) —
consistent with how relational databases typically model one-to-many data
(one patient document, many tests). We followed the literal attached
deliverable rather than the prose description, since the schema file is
the more concrete and more likely accurate target.

**T3. The provided schema contains overlapping/duplicate-looking
columns** (e.g. both `medicine` and `medication_medicine`; both `page_no`,
`page_number`, and `report_details_page_no`). This looks like an
auto-generated "union of every field ever seen across formats" schema
rather than a hand-curated one. Rather than silently dropping client-
provided columns, we kept all of them and populated the cleanest/most
direct version of each concept (`medicine`, `page_number`, etc.), leaving
the apparent-duplicate columns NULL. A production conversation with the
client would clarify which columns are still needed.

**T4. Fuzzy matching uses Python's built-in `difflib.SequenceMatcher`**
rather than an external library (`rapidfuzz`, `fuzzywuzzy`) or an LLM-based
approach (e.g. Gemini, which the assignment explicitly permits). This was a
time/dependency trade-off for the take-home scope — `difflib` needs no
extra install and is fast enough at this data volume. In production, an
LLM-based or embedding-based matcher would likely resolve more edge cases —
flagged as the highest-leverage next improvement (see Scope Exclusions).

**T5. The test name dictionary was seeded, then expanded against the
real sample data.** The provided `Clinical_name_standardization.xlsx` gave
19 canonical groupings as a starting seed. After running the pipeline
against all 5 sample files and reviewing every "unresolved" test name, the
dictionary was expanded to ~60 canonical tests, and a separate explicit
list of known non-lab terms (vital signs like BP/Temp/Pulse, panel headers
like "LIVER FUNCTION TEST(LFT)", placeholder junk) was added so these are
correctly classified as "not a lab test" rather than counted as dictionary
gaps. This brought genuinely unresolved test names on the sample data to
**zero**. Real production data will introduce new test types over time;
the config-file design (NFR-2.1) makes extending this an ops task.

**T6. "Unresolved" and "known non-lab term" are tracked as two distinct
classifications.** Early in development, vitals and panel headers
appeared in the same "Unresolved Test Names" dashboard panel as genuine
dictionary gaps, making the panel misleading (128 items, most not
actionable). Splitting these into `normalization_method: "non_lab_term"`
vs `"unresolved"` makes the panel an honest, actionable worklist.

**T7. Clinic-level data quality percentage is clamped to 0-100%.**
The formula `100 - (invalid + unresolved) / total * 100` can mathematically
go negative for a very messy source file. A negative quality percentage is
confusing to read, so the UI clamps it to 0% as a floor.

**T8. "Duplicate Files Caught" counts genuine duplicates, not idempotent
re-runs.** Found while testing: re-running the pipeline multiple times on
an unchanged folder caused the duplicate count to keep growing on every
run, even though only one file is actually a duplicate of another. Root
cause: re-processing an already-loaded file also triggers the "is this
content already in the database" check — necessary for idempotency
(NFR-3.2), but not a *new* duplicate discovery. Fixed by tracking every
filename ever encountered (loaded or caught as duplicate) in its own
table, so a re-run is correctly distinguished from a genuinely new
duplicate. **Verified:** running the pipeline 4 times in a row on the same
folder, the genuine duplicate count stays at exactly 1 throughout.

---

## C. Data Assumptions
*How we handled edge cases in the sample data; what we'd need to know
before going to production.*

**D1. Redacted fields are treated as intentionally missing, not
malformed.** Fields like `[AGE REDACTED]`, `[GENDER REDACTED]`, `[PATIENT
NAME REDACTED]` appear throughout the sample data. The demographic
normaliser recognises this bracket pattern and returns `None` cleanly,
rather than parsing it as a real value or flagging it as an error. Before
production, we'd need to confirm with the client whether redaction happens
upstream (as in these samples) or whether the pipeline itself must perform
PII masking (NFR-4.3) — these are different responsibilities.

**D2. Combined multi-value result strings are flagged, not auto-split.**
`Sample_JSON_file5.json` contains rows like `"test_name": "LFT ( SGOT - 38,
SGPT -14, ALP - 127)"` with an empty `result` field — three distinct lab
values embedded in the test name itself. We chose not to attempt
automatic splitting, because doing so without knowing the exact panel
structure risks silently assigning the wrong value to the wrong test — a
worse outcome than flagging it for review. Logged as
`value_type: combined_value` / `test_analytics: INVALID` and surfaced in
the Flagged Records Queue.

**D3. A test name matching our dictionary but carrying an unexpected
unit is treated as a data integrity problem, not a normal result.**
Discovered directly while testing on `Sample_JSON_file2.json`: a row whose
`test_name` matched "HAEMOGLOBIN" via exact dictionary lookup carried a
`unit` of `cells/cu.mm` — a cell-count unit that doesn't apply to
haemoglobin (a concentration test, measured in g/dL). This strongly
suggests the source extraction shifted/misaligned columns across several
consecutive rows. Rather than confidently comparing a mismatched value
against the wrong reference range, the validator flags these `INVALID`
whenever a row's unit doesn't match the expected unit for its matched
canonical test.

**D4. Placeholder/template rows are recognised and skipped.**
`Sample_JSON_file5.json` contains a literal template row
(`"test_name": "test_name", "result": "result", ...`) and a placeholder
date (`"reports_date": "DD/MM/YYYY"`). These are explicitly recognised (the
date normaliser rejects known placeholder strings) and otherwise fail to
fuzzy-match anything meaningful, so they correctly end up
`UNRESOLVED`/`INVALID` rather than being silently loaded as genuine
results.

**D5. Vitals mixed into lab report rows** (Temp, Pulse, BP, SpO2, weight
— seen in `Sample_JSON_file5.json`) are recognised via the curated
non-lab-terms list (see Technical Assumption T6) rather than miscounted as
dictionary gaps. In production, distinguishing "vitals" from "lab tests" as
a first-class category would be a worthwhile config-level addition.

**D6. The same source file can internally contradict itself on a
test's reference range — and this is surfaced, not silently resolved.**
`Sample_JSON_file4.json` reports GLOBULIN six separate times across
different pages of the same report, with two different reference ranges
appearing across those repeats (2.0-3.5 g/dL on some pages, 3.5-5.0 g/dL on
others) for the identical result value (3.7). Rather than picking one
range as "correct," the pipeline validates each row against its own
row-level range as provided — meaning the same test/value pair can
legitimately show up as both `WITHIN RANGE` and `ABOVE RANGE` in different
rows from the same file. This is a genuine ambiguity in the source data,
not a pipeline defect. A production system would want a config-level rule
for which range "wins" when a single source contradicts itself.

---

## D. Scope Exclusions
*What we consciously left out, why, and what it would take to include it.*

| Exclusion | Why left out | Effort to add |
|---|---|---|
| **GCS integration** — reads from a local folder instead of a real bucket | Take-home time budget; identical logic either way | A few hours — replace `Path.glob()` with a `google-cloud-storage` client |
| **LLM/GenAI-based test name resolution** — uses string-similarity fuzzy matching instead | Assignment permits but doesn't require GenAI; simpler approach sufficient for take-home scope | Swap `difflib` calls for an LLM/embedding call in `test_name_normaliser.py`; highest-leverage next improvement for NFR-4.1 |
| **PII tokenisation/masking (NFR-4.3)** | Sample data already arrives pre-redacted by the source system | A masking/tokenisation step at the ingestion layer, before any downstream storage |
| **Automatic splitting of combined multi-value result strings** | Risk of silently assigning the wrong value to the wrong test (see D2) | Per-known-panel config (e.g. "LFT panel always reports SGOT, SGPT, ALP in that order") |
| **Schema versioning per clinic (NFR-2.3)** | Marked Optional; out of 12-24 hour build window | A `schema_version` field per clinic config + version-aware parsing |
| **Authentication/authorisation on the Operational UI** | Out of scope for a local take-home demo | Standard auth middleware (e.g. Flask-Login) behind the org's SSO |
| **Production-grade WSGI server for the UI** | Flask's dev server is sufficient for this demo | Gunicorn/uWSGI behind a reverse proxy — a deployment config change, not a code change |
