"""
ingest.py — minimal SEC filing ingestion into SQLite.

Reads facts.json (SEC XBRL company facts) and writes:
  companies  — one row for the filer
  documents  — one row for this filing
  figures    — one row per quarterly value we care about

Safe to re-run: duplicate rows are ignored.
"""

import json
import sqlite3
from datetime import date

# ---------------------------------------------------------------------------
# Configuration — the only things you ever need to edit
# ---------------------------------------------------------------------------

FACTS_FILE = "facts.json"
DB_FILE = "app.db"

TICKER = "NVDA"
ACCESSION = "0001045810-26-000075"     # the filing to ingest
REVENUE_TAG = "Revenues"           # XBRL tag for revenue
METRIC = "revenue"                     # our own field name

DOCUMENT = {
    "title": "NVIDIA CORP 10-Q",
    "form_type": "10-Q",
    "date": "2026-08-26",
    "url": "https://www.sec.gov/Archives/edgar/data/1045810/"
           "000104581026000075/nvda-20260726.htm",
    "file_path": "nvda-20260726.htm",
}

MIN_DAYS, MAX_DAYS = 80, 100           # a quarter is ~90 days

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    company_id INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    ticker     TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    report_id  INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(company_id),
    title      TEXT,
    form_type  TEXT,
    date       TEXT,
    url        TEXT,
    file_path  TEXT,
    UNIQUE (company_id, form_type, date)
);

CREATE TABLE IF NOT EXISTS figures (
    figure_id  INTEGER PRIMARY KEY,
    report_id  INTEGER NOT NULL REFERENCES documents(report_id),
    statement  TEXT,
    metric     TEXT NOT NULL,
    period     TEXT NOT NULL,
    value      REAL NOT NULL,
    unit       TEXT NOT NULL,
    page       INTEGER,
    UNIQUE (report_id, metric, period)
);
"""

# ---------------------------------------------------------------------------
# Reading the facts
# ---------------------------------------------------------------------------

def is_quarterly_span(fact):
    """True for duration facts covering about three months."""
    if "start" not in fact:
        return False                   # instant fact: a point in time, not a span
    days = (date.fromisoformat(fact["end"])
            - date.fromisoformat(fact["start"])).days
    return MIN_DAYS <= days <= MAX_DAYS


def quarterly_values(data, tag, accession):
    """This filing's quarterly USD values for one tag, newest first."""
    us_gaap = data["facts"]["us-gaap"]

    if tag not in us_gaap:
        candidates = [t for t in us_gaap if "Revenue" in t]
        raise SystemExit(
            f"Tag '{tag}' not found.\nRevenue-like tags available:\n  "
            + "\n  ".join(candidates)
        )

    usd_facts = us_gaap[tag]["units"].get("USD", [])

    values = [
        fact for fact in usd_facts
        if fact.get("accn") == accession and is_quarterly_span(fact)
    ]

    # ISO dates sort chronologically even as plain strings.
    values.sort(key=lambda fact: fact["end"], reverse=True)
    return values

# ---------------------------------------------------------------------------
# Writing to SQLite
# ---------------------------------------------------------------------------

def main():
    with open(FACTS_FILE) as file:
        data = json.load(file)

    connection = sqlite3.connect(DB_FILE)
    cursor = connection.cursor()
    cursor.executescript(SCHEMA)

    # 1. The filer — taken from the file's own identity.
    cursor.execute(
        "INSERT OR IGNORE INTO companies (company_id, name, ticker) VALUES (?, ?, ?)",
        (data["cik"], data["entityName"], TICKER),
    )

    # 2. This filing. The lookup keeps re-runs from adding duplicates.
    cursor.execute(
        """INSERT OR IGNORE INTO documents
               (company_id, title, form_type, date, url, file_path)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (data["cik"], DOCUMENT["title"], DOCUMENT["form_type"],
         DOCUMENT["date"], DOCUMENT["url"], DOCUMENT["file_path"]),
    )
    report_id = cursor.execute(
        "SELECT report_id FROM documents WHERE company_id=? AND form_type=? AND date=?",
        (data["cik"], DOCUMENT["form_type"], DOCUMENT["date"]),
    ).fetchone()[0]

    # 3. One row per quarterly value, linked to the filing.
    for fact in quarterly_values(data, REVENUE_TAG, ACCESSION):
        cursor.execute(
            """INSERT OR IGNORE INTO figures
                   (report_id, metric, period, value, unit)
               VALUES (?, ?, ?, ?, ?)""",
            (report_id, METRIC, fact["end"], fact["val"], "USD"),
        )

    connection.commit()

    print("companies:")
    for row in cursor.execute("SELECT * FROM companies"):
        print("  ", row)
    print("figures:")
    for row in cursor.execute(
        "SELECT metric, period, value, unit FROM figures ORDER BY period DESC"
    ):
        print("  ", row)

    connection.close()


if __name__ == "__main__":
    main()