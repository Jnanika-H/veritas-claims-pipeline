# Solution Architecture
## Veritas Claims — Medical Data Standardisation Pipeline

---

## 1. Overview

Veritas Claims receives 200,000+ JSON medical reports daily from 500+
clinics, each with its own field names, units, date formats, and test
naming conventions. This document describes a pipeline that ingests these
raw files, standardises them into a clean canonical schema, validates
results against medical reference ranges, loads them into a queryable
database, and exposes an operational dashboard for the ops team.

The prototype in this repository implements this architecture at small
scale, using a local folder in place of cloud storage and SQLite in place
of a managed database. The logic is identical either way — only the
infrastructure underneath changes.

**See the diagram:** `docs/veritas_architecture.drawio` (open at
app.diagrams.net) or the exported PNG in the accompanying Google Slides
deck.

---

## 2. Architecture Diagram (text version)

```
                    CLINICS / LABS
              (500+ sources, own JSON format)
                          |
                          v  JSON files
            +------------------------------+
            |   1. INGESTION LAYER         |
            |   GCS bucket, event-triggered|
            +--------------+---------------+
                            |  event per file
                            v
            +-----------------------------------+
            |  2. PROCESSING LAYER              |
            |  Parse -> Dedup -> Standardise ->  |
            |  Validate                          |
            +-------+-------------------+--------+
              clean rows            failures
                    v                   v
        +-------------------+   +---------------------+
        | 3. STORAGE LAYER  |   | DEAD-LETTER STORE    |
        | canonical schema  |   | error_log + alerts   |
        +---------+---------+   +----------+-----------+
                   |                        |
                   +-----------+------------+
                                v
            +-----------------------------------+
            |  4. CONFIGURATION LAYER           |
            |  versioned JSON/YAML, no-code      |
            |  clinic onboarding                 |
            +--------------+--------------------+
                            v
            +------------------------------------+
            |  5. UI LAYER                       |
            |  Operational dashboard (Flask)      |
            +------------------------------------+

     (cross-cutting, connects to every layer above)
            +------------------------------------+
            |  MONITORING / OBSERVABILITY         |
            |  Cloud Monitoring + Logging          |
            +------------------------------------+
```

---

## 3. Functional Requirements (FR1-5) — Narrative

### FR-1: Ingestion Layer
**How JSON files land from clinics into GCS (or equivalent), and how the
pipeline reads them.**

In production, files land in a GCS bucket organised by
`{clinic_id}/{date}/{file}.json`. A Cloud Storage event notification
triggers processing per file via Pub/Sub into a Cloud Run job. In this
prototype, `src/ingestion/ingest.py` reads from a local folder instead —
identical logic, different source.

- **FR-1.1 Multi-source ingestion:** `read_json_files()` discovers every
  `.json` file in the input folder (or a single named file via `--file`).
- **FR-1.2 Duplicate detection:** `src/ingestion/dedup.py` fingerprints
  the *clinical content only* (not envelope fields like `documentId`), so
  the same encounter resubmitted under a new ID is still caught. Verified
  directly on the provided sample data — `Sample_JSON_file3.json` is
  byte-different but clinically identical to `Sample_JSON_file1.json`, and
  is correctly skipped.
- **FR-1.3 Schema flexibility:** the pipeline looks for a `classifier`
  field per content block (`lab_report` / `discharge_summary`) and only
  processes recognised block types. A malformed file is logged and skipped
  — it never blocks the rest of the batch.

### FR-2: Processing / Standardisation Layer
**How files are picked up, parsed, standardised.**

`src/standardisation/orchestrator.py` coordinates four sub-modules:

| Requirement | Module | What it does |
|---|---|---|
| FR-2.1 Test name normalisation | `test_name_normaliser.py` | Exact dictionary lookup first; falls back to fuzzy string matching (`difflib`) for unseen variants; honestly reports "unresolved" rather than guessing |
| FR-2.2 Output schema | `storage/db_loader.py` | Populates the client-provided canonical schema (`docs/Ourput-table-ideal-schema.csv`) — see Section 6 for why this schema (long/normalised) was used over the prose description (pivoted) |
| FR-2.3 Numeric conversion | `numeric_unit_normaliser.py` | Extracts clean numeric values from messy strings (e.g. "120000 cells/cu.mm" to 120000.0); detects qualitative results, ranges-in-result-field, and combined multi-value strings without guessing |
| FR-2.4 Unit harmonisation | `numeric_unit_normaliser.py` (UnitNormaliser) | Resolves unit spelling variants (g/dl vs gm/dL) and applies scale conversion factors where units differ by more than spelling |
| FR-2.5 Demographic normalisation | `demographic_normaliser.py` | Age ("33Y11M26D" to structured), gender (M/Male to MALE), dates (multiple formats to ISO 8601); recognises redacted placeholders cleanly |
| FR-2.6 Medicine name mapping | `medicine_mapping.json` + orchestrator | Brand to generic mapping, punctuation-tolerant matching ("TAB. DOLO 650" and "TAB DOLO 650" both resolve) |

### FR-3: Validation & Analytics Layer
**Range checks, outlier detection, analytics classification.**

`src/validation/validator.py`:
- **FR-3.1 Range validation:** every numeric result is compared against
  `config/reference_ranges.json`.
- **FR-3.2 Outlier detection:** checked *before* plain above/below range —
  a value beyond `outlier_low`/`outlier_high` (medically implausible) is
  labelled `OUTLIER`, a stronger flag than `ABOVE RANGE`/`BELOW RANGE`.
- **FR-3.3 Analytics classification:** every lab result row gets exactly
  one of `WITHIN RANGE`, `ABOVE RANGE`, `BELOW RANGE`, `OUTLIER`, `INVALID`,
  or blank (for non-numeric/not-applicable results).
- **FR-3.4 Incorrect value flagging:** combined multi-value strings and
  unit/test-name mismatches are flagged `INVALID` rather than silently
  validated against the wrong reference. (See Section 6 for the real
  unit-mismatch bug this caught in `Sample_JSON_file2.json`.)

### FR-4: Output & Storage Layer
**Database choice and schema design.**

`src/storage/db_loader.py` loads into SQLite (prototype) using the exact
column list from `docs/Ourput-table-ideal-schema.csv`.

- **FR-4.1 Structured DB load:** confirmed column-for-column match to the
  client-provided schema.
- **FR-4.2 Error logging:** a separate `error_log` table records files that
  fail to parse, with the error message — visible on the dashboard.
- **FR-4.3 Audit trail:** every row carries `file_gcs_path`, `document_id`,
  `trace_id`, and `ingested_at`, so any standardisation decision traces back
  to its exact source file and moment of processing.

**Idempotency (also satisfies NFR-3.2):** each row's database ID is a hash
of its own content (document ID, file path, test name, page number) — not
a random UUID. Re-running the pipeline on the same input overwrites the
same rows instead of duplicating them. **Verified directly:** running the
pipeline twice on the 5 sample files leaves the row count at exactly 402
both times.

### FR-5: Operational UI Layer
**How the dashboard is served.**

`src/ui/app.py` — a Flask app reading directly from the canonical
database:

- **FR-5.1 Pipeline dashboard** (`/`) — files processed, rows standardised,
  files failed, duplicates caught, records flagged, result classification
  breakdown.
- **FR-5.2 Record inspector** (`/record/<id>`) — every field of any single
  standardised row.
- **FR-5.3 Flagged records review** (`/flagged`) — filterable queue of
  outliers, out-of-range, and invalid records.
- **FR-5.4 Clinic-level summary** (`/clinics`) — per-source quality
  metrics (total rows, invalid count, outliers, unresolved names, a clamped
  0-100% quality score).

In production this runs as its own Cloud Run service in front of a read
replica of the production database, so dashboard queries never compete
with the processing layer for write throughput.

---

## 4. Non-Functional Requirements (NFR1-5) — Tech & Concept Notes

### NFR-1: Scale & Performance
- **NFR-1.1 Throughput (200k/day, 2x burst):** the processing layer is a
  stateless, horizontally-scalable Cloud Run service (or Dataflow
  pipeline) — each file is processed independently, so additional
  instances increase throughput roughly linearly.
- **NFR-1.2 Latency (15 min p95):** realistic for an event-triggered
  design — there's no batch-window delay, since each file triggers its own
  processing run on arrival. Main risk under burst load is database write
  contention, mitigated by small batched transactions and a database
  (BigQuery / Postgres with pooling) built for high-throughput concurrent
  writes.
- **NFR-1.3 Horizontal scalability:** architecturally simple — there is no
  shared mutable state between file-processing runs except the database
  itself, which is the natural scaling bottleneck and is handled by
  choosing a database designed for concurrent writes at this volume.

### NFR-2: Clinic Onboarding
- **NFR-2.1 Zero-code onboarding (Required):** all clinic-specific
  knowledge lives in `/config/*.json` — adding a new clinic's naming
  quirks is a config edit, not a code change. **Verified directly:** a
  brand-new test file with a never-seen misspelling ("Hemglobin") was
  auto-corrected to HAEMOGLOBIN via fuzzy matching with zero config or
  code changes.
- **NFR-2.2 Onboarding time (1 business day):** the fuzzy-matching
  fallback auto-resolves most never-seen variants for free; remaining
  gaps surface on the dashboard's "Unresolved Test Names" panel as a
  direct, actionable worklist — adding entries there is a few minutes per
  test, well within a business day.
- **NFR-2.3 Schema versioning:** not implemented (Optional, out of
  take-home scope — see Scope Exclusions in ASSUMPTIONS.md).

### NFR-3: Reliability & Resilience
- **NFR-3.1 Fault tolerance (Required):** one malformed file never blocks
  others — demonstrated directly: a deliberately corrupted JSON file is
  logged to `error_log` and skipped while the rest of the batch proceeds.
- **NFR-3.2 Idempotency (Required):** see FR-4 above — verified by running
  the full pipeline twice on identical input; row count unchanged.
- **NFR-3.3 Availability (99.5% uptime):** not applicable to a take-home
  prototype; in production this comes from Cloud Run's built-in redundancy
  plus a managed database's documented SLA.

### NFR-4: Data Quality & Governance
- **NFR-4.1 Standardisation coverage (98% target, Required):** tracked
  live via the dashboard's "Unresolved Test Names" panel. On the 5 provided
  sample files, after dictionary expansion, genuinely unresolved test
  names is **0** (down from 128 before expansion) — see ASSUMPTIONS.md.
- **NFR-4.2 Data lineage (Required):** every row carries `document_id`,
  `file_gcs_path`, `trace_id`, and `ingested_at` — any row traces back to
  its exact source file and clinic.
- **NFR-4.3 PII handling:** treated as a Scope Exclusion (see
  ASSUMPTIONS.md) — sample data arrives pre-redacted by the source system.

### NFR-5: Observability
- **NFR-5.1 Monitoring & alerting (Required):** the prototype logs
  structured per-file summaries via Python's `logging` module (files
  processed, rows produced, rows flagged). In production these feed Cloud
  Monitoring, with alerts on error rate > 1% or lag > SLA.
- **NFR-5.2 Logging (Required):** in production, every log line carries a
  `correlation_id` per record so a single record's journey through
  ingestion to standardisation to validation to storage is traceable
  end-to-end in Cloud Logging.

---

## 5. Architectural Pattern Choices

- **Batch-on-arrival, not streaming.** Each file is processed independently
  and atomically as it arrives (or is discovered in a folder scan) — this
  is closer to a micro-batch / event-triggered pattern than true streaming.
  Justification: medical reports arrive as discrete documents, not a
  continuous event stream that needs windowing or aggregation — there's no
  natural "stream" semantic to exploit here, so the added complexity of a
  streaming framework (windowing, watermarks) buys nothing.
- **Schema-on-write, not schema-on-read.** Every file is standardised into
  the canonical schema *before* it's queryable. Justification: the whole
  point of this pipeline is to make cross-clinic comparison possible (FR-2)
  — that requires the data to already be in one consistent shape at query
  time, not reinterpreted per-query.
- **Config-over-code for all clinic-specific logic.** Directly satisfies
  NFR-2.1; also the single biggest lever for keeping the system extensible
  without developer involvement for every new clinic.

---

## 6. Assumptions (Summary)

Full structured assumptions are in `docs/ASSUMPTIONS.md`. Key points
repeated here per the assignment's request for an explicit Assumptions
section in the architecture document itself:

- **Source:** JSON files arrive in a GCS bucket, organised by clinic ID and
  date (as stated in the assignment).
- **Database:** canonical output loads into any relational/columnar store;
  this prototype uses SQLite, production would use BigQuery or PostgreSQL
  with no change to row-building logic.
- **Managed cloud services vs open-source equivalents** are treated as
  interchangeable at the architecture level — the diagram names GCP
  services as a concrete example, not a hard requirement.
- **Output schema:** the client-provided schema file (long/normalised, one
  row per test) was followed over the assignment's prose description
  (pivoted, 5-columns-per-test) — see ASSUMPTIONS.md, Technical Assumption 2,
  for full reasoning.
- **Unit-mismatch validation:** a real data-integrity bug (test name
  matching but unit mismatching) was found in `Sample_JSON_file2.json`
  during testing and is now explicitly validated against — see
  ASSUMPTIONS.md, Data Assumption 3.
