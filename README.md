# Veritas Claims — Medical Data Standardisation Pipeline

A prototype pipeline that takes messy JSON medical reports from clinics,
cleans them up into a consistent format, checks the results against medical
reference ranges, stores everything in a database, and gives the ops team a
simple dashboard to keep an eye on it.

Built for the Veritas Claims Analytics data science internship take-home
assignment at Niveus Solutions.

---

## 1. Setup Instructions

```bash
# 1. Install dependencies
pip install -r requirements.txt
# (add --break-system-packages if your system needs it, or use a venv — see below)

# 2. Run the pipeline on the sample data
cd src
python run_pipeline.py --input ../sample-data --db ../veritas.db

# 2b. Or run it on just one file (handy for testing a single submission,
#     or showing off how a brand new clinic gets handled)
python run_pipeline.py --file ../sample-data/Sample_JSON_file4.json --db ../veritas.db

# 3. Run the dashboard
python ui/app.py --db ../veritas.db --port 5000
# then open http://localhost:5000

# 4. Run the tests
cd ..
python -m pytest tests/ -v
```

**If you'd rather use a virtual environment:**
```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

## 2. Architecture Summary

```
sample JSON files -> INGEST -> STANDARDISE -> VALIDATE -> STORE (SQLite) -> view in UI
```

Five stages, each doing one job:

- **Ingest** (`src/ingestion/`) reads the JSON files, simulating a GCS
  bucket. A broken file gets logged and skipped rather than taking down the
  whole run, and it catches duplicate submissions even when they come in
  under a different document ID.
- **Standardise** (`src/standardisation/`) is where most of the actual work
  happens — fixing misspelled test names (dictionary lookup, with fuzzy
  matching as a backup), pulling clean numbers out of messy result strings,
  converting units, tidying up age/gender/date fields, and mapping brand
  medicines to their generic names.
- **Validate** (`src/validation/`) compares each numeric result to a normal
  range and labels it `WITHIN RANGE`, `ABOVE RANGE`, `BELOW RANGE`,
  `OUTLIER`, or `INVALID`.
- **Store** (`src/storage/`) writes everything into SQLite, matching the
  client's provided schema column for column. Running the pipeline twice
  on the same files never creates duplicate rows.
- **Operational UI** (`src/ui/`) is a small Flask app with four pages: a
  dashboard, a flagged-records queue, per-clinic quality stats, and a
  record inspector.

The full architecture diagram and the reasoning behind the non-functional
requirements (scale, reliability, observability, etc.) live in
`docs/ARCHITECTURE.md`.

### Project Structure
```
veritas-pipeline/
├── src/
│   ├── ingestion/         # file reading, duplicate detection
│   ├── standardisation/   # test name, numeric/unit, demographic normalisers
│   ├── validation/        # range checks, outlier detection
│   ├── storage/           # SQLite loader matching the canonical schema
│   ├── ui/                # Flask operational dashboard
│   └── run_pipeline.py    # main entry point
├── config/                 # editable JSON files — add a clinic, test, unit,
│   │                        or medicine without touching the code
│   ├── test_name_mapping.json
│   ├── reference_ranges.json
│   ├── unit_mapping.json
│   └── medicine_mapping.json
├── sample-data/            # the 5 provided sample JSON files
├── tests/                  # unit tests for standardisation, validation, dedup
└── docs/
    ├── ARCHITECTURE.md
    ├── ASSUMPTIONS.md
    ├── veritas_architecture.drawio
    ├── Ourput-table-ideal-schema.csv      # the client's target schema
    └── Clinical_name_standardization.xlsx # the client's test dictionary seed
```

---

## 3. Key Design Decisions

**Prefer "I don't know" over a confident wrong answer.** Every step in the
standardisation process tries an exact match first, then fuzzy matching,
and if neither works it honestly says so instead of forcing a guess.
Unresolved names show up on the dashboard for someone to review later.

**Duplicates are caught by content, not by file.** `Sample_JSON_file1.json`
and `Sample_JSON_file3.json` have the same diagnosis and medications, but
different document IDs — exactly the "submitted twice from different
systems" scenario the assignment describes. Hashing the whole file would
have missed this, so the dedup logic only hashes the actual clinical
content (see `src/ingestion/dedup.py`).

**Re-running the pipeline doesn't duplicate anything.** Each row's database
ID comes from its own content (document ID, file, test name, page number)
rather than a random UUID, so loading the same file twice just overwrites
the same row. I tested this directly — running the pipeline twice in a row
on the sample data leaves the row count exactly where it started.

**A test that matches by name but has the wrong unit gets flagged, not
trusted.** While testing on `Sample_JSON_file2.json` I found a row where
the test name matched "HAEMOGLOBIN" correctly, but its unit was a
cell-count unit that has nothing to do with haemoglobin — a sign the source
file's columns had shifted. Rather than silently comparing that value
against the wrong range, the validator now catches this case.

**Everything clinic-specific lives in config, not code.** Test name
variants, units, reference ranges, and medicine names are all in
`/config/*.json`. Onboarding a new clinic is an edit to those files, not a
code change.

The full reasoning behind these decisions, and a few others, is in
`docs/ASSUMPTIONS.md`.

---

## 4. Known Limitations

- Reads from a local folder instead of an actual GCS bucket — swapping
  this out is a fairly small change, explained in `docs/ASSUMPTIONS.md`.
- The test name dictionary covers about 60 canonical tests, built up by
  going through every test name across all 5 sample files. Nothing in the
  sample data comes back unresolved anymore, but a real production system
  will keep seeing new test types over time — the dashboard's "Unresolved
  Test Names" panel is meant to be the ongoing worklist for that.
- Fuzzy matching uses Python's built-in `difflib` rather than something
  LLM-based. The assignment allows for GenAI here, and that's probably the
  single best upgrade to make next — it would likely resolve more edge
  cases than a plain string-similarity check can.
- Combined result strings like `"LFT ( SGOT - 38, SGPT -14, ALP - 127)"`
  get flagged rather than automatically split into separate tests, since
  guessing wrong there felt riskier than just flagging it.
- The dashboard runs on Flask's built-in development server, which is fine
  for this demo but not something you'd run in production.
- No login on the dashboard — reasonable for a local take-home, not for
  anything handling real patient data.

---

## 5. Running the Tests

```bash
python -m pytest tests/ -v
```

53 tests across three files:
- `tests/test_standardisation.py` — name matching (exact, fuzzy,
  unresolved, known non-medical terms), pulling numbers out of messy
  strings, and demographic cleanup.
- `tests/test_validation.py` — range checks, the outlier-vs-plain-out-of-
  range distinction, and the unit-mismatch case mentioned above.
- `tests/test_dedup.py` — duplicate detection, including a bug I found
  where re-running the pipeline kept inflating the duplicate count even
  though nothing new was actually duplicated.

---

## Diagram and Slides

The architecture diagram is in `docs/veritas_architecture.drawio` (open it
at app.diagrams.net). An exported version of it, along with technical
notes and references, is in the accompanying Google Slides deck.
