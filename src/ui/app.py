"""
Operational UI -- Backend (FR-5.1 through FR-5.4)
----------------------------------------------------
A lightweight Flask app that reads directly from the SQLite database the
pipeline writes to, and serves:

    FR-5.1  /              -- Pipeline dashboard (totals: processed, failed, flagged)
    FR-5.2  /record/<id>   -- Record inspector (raw-ish + standardised side by side)
    FR-5.3  /flagged       -- Flagged records review queue
    FR-5.4  /clinics       -- Per-clinic-ish (per source file) quality stats

NOTE on "clinic-level": the sample data doesn't carry an explicit clinic_id
field (clinics aren't named in the sample JSONs -- see Assumptions doc), so
this prototype groups by `source_system` / `file_gcs_path` as the closest
available proxy for "where did this come from". In production with real
clinic identifiers, swap the GROUP BY to the real clinic_id column --
no other code changes needed.

Run with:  python src/ui/app.py --db ../veritas.db
Then open: http://localhost:5000
"""

import argparse
import sqlite3
import sys
from pathlib import Path
from flask import Flask, render_template, g, request, jsonify

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from storage.db_loader import DatabaseLoader
from ingestion.dedup import DuplicateDetector

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["DB_PATH"] = "veritas.db"


def ensure_database_ready(db_path: str):
    """
    Makes sure the database file and every table the dashboard queries
    exist before the UI tries to read them. Without this, starting the UI
    against a brand-new (or never-run) database path crashes with
    'no such table: ...' instead of showing a clean empty state -- found
    while testing the UI standalone, without running the pipeline first.

    Reuses DatabaseLoader's and DuplicateDetector's own table-creation
    logic rather than duplicating schema definitions here, so they stay in
    sync automatically as those modules evolve.
    """
    loader = DatabaseLoader(db_path)
    loader.close()
    dedup = DuplicateDetector(db_path)
    dedup.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DB_PATH"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.route("/")
def dashboard():
    db = get_db()

    total_files = db.execute(
        "SELECT COUNT(DISTINCT file_gcs_path) as cnt FROM standardised_records"
    ).fetchone()["cnt"]

    total_rows = db.execute(
        "SELECT COUNT(*) as cnt FROM standardised_records"
    ).fetchone()["cnt"]

    failed_files = db.execute(
        "SELECT COUNT(*) as cnt FROM error_log"
    ).fetchone()["cnt"]

    duplicate_files = db.execute(
        "SELECT COUNT(*) as cnt FROM duplicate_log WHERE is_genuine_duplicate = 1"
    ).fetchone()["cnt"]

    flagged_count = db.execute(
        "SELECT COUNT(*) as cnt FROM standardised_records "
        "WHERE test_analytics IN ('OUTLIER', 'ABOVE RANGE', 'BELOW RANGE', 'INVALID')"
    ).fetchone()["cnt"]

    analytics_breakdown = db.execute(
        "SELECT test_analytics, COUNT(*) as cnt FROM standardised_records "
        "WHERE record_type = 'LAB_TEST_RESULT' GROUP BY test_analytics ORDER BY cnt DESC"
    ).fetchall()

    record_type_breakdown = db.execute(
        "SELECT record_type, COUNT(*) as cnt FROM standardised_records GROUP BY record_type"
    ).fetchall()

    unresolved_names = db.execute(
        "SELECT DISTINCT test_name_original FROM standardised_records "
        "WHERE normalization_method = 'unresolved' LIMIT 20"
    ).fetchall()

    return render_template(
        "dashboard.html",
        total_files=total_files,
        total_rows=total_rows,
        failed_files=failed_files,
        duplicate_files=duplicate_files,
        flagged_count=flagged_count,
        analytics_breakdown=analytics_breakdown,
        record_type_breakdown=record_type_breakdown,
        unresolved_names=unresolved_names,
    )


@app.route("/flagged")
def flagged_queue():
    db = get_db()
    analytics_filter = request.args.get("type", "ALL")

    query = (
        "SELECT id, document_id, file_gcs_path, test_name_original, "
        "test_name_canonical, result_value, result_text, unit_canonical, "
        "range_low, range_high, test_analytics, normalization_method, "
        "normalization_confidence FROM standardised_records "
        "WHERE test_analytics IN ('OUTLIER', 'ABOVE RANGE', 'BELOW RANGE', 'INVALID')"
    )
    params = []
    if analytics_filter != "ALL":
        query += " AND test_analytics = ?"
        params.append(analytics_filter)
    query += " ORDER BY test_analytics, file_gcs_path LIMIT 200"

    rows = db.execute(query, params).fetchall()
    return render_template("flagged.html", rows=rows, current_filter=analytics_filter)


@app.route("/clinics")
def clinic_stats():
    db = get_db()

    # Grouped by source file as a proxy for clinic (see module docstring).
    rows = db.execute("""
        SELECT
            file_gcs_path,
            COUNT(*) as total_rows,
            SUM(CASE WHEN test_analytics = 'INVALID' THEN 1 ELSE 0 END) as invalid_count,
            SUM(CASE WHEN test_analytics = 'OUTLIER' THEN 1 ELSE 0 END) as outlier_count,
            SUM(CASE WHEN normalization_method = 'unresolved' THEN 1 ELSE 0 END) as unresolved_names,
            SUM(CASE WHEN test_name_original IS NOT NULL AND test_name_original != '' THEN 1 ELSE 0 END) as total_named_tests
        FROM standardised_records
        GROUP BY file_gcs_path
        ORDER BY file_gcs_path
    """).fetchall()

    return render_template("clinics.html", rows=rows)


@app.route("/record/<record_id>")
def record_inspector(record_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM standardised_records WHERE id = ?", (record_id,)
    ).fetchone()
    if row is None:
        return "Record not found", 404
    return render_template("record_inspector.html", row=dict(row))


@app.route("/search")
def search_records():
    db = get_db()
    q = request.args.get("q", "").strip()
    rows = []
    if q:
        rows = db.execute(
            "SELECT id, document_id, file_gcs_path, record_type, test_name_canonical, "
            "patient_name, uhid FROM standardised_records "
            "WHERE document_id LIKE ? OR uhid LIKE ? OR patient_name LIKE ? "
            "LIMIT 50",
            (f"%{q}%", f"%{q}%", f"%{q}%"),
        ).fetchall()
    return render_template("search.html", rows=rows, query=q)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Veritas Claims Operational UI")
    parser.add_argument("--db", default="veritas.db", help="Path to SQLite database file")
    parser.add_argument("--port", default=5000, type=int)
    args = parser.parse_args()

    app.config["DB_PATH"] = str(Path(args.db).resolve())
    ensure_database_ready(app.config["DB_PATH"])
    app.run(debug=True, port=args.port)
