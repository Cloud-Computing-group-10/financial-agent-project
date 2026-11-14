"""SQLite schema (plan section 7 + v2 additions in 15.2)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    company_id INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    ticker     TEXT NOT NULL UNIQUE,
    cik        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    report_id  INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(company_id),
    title      TEXT,
    form_type  TEXT NOT NULL,
    date       TEXT,          -- filing date
    period     TEXT,          -- fiscal quarter covered, e.g. 'Q2 2023'
    period_end TEXT,          -- report date
    accession  TEXT NOT NULL UNIQUE,
    url        TEXT,
    file_path  TEXT,          -- original primary document on disk (NULL if text not indexed)
    has_text   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS figures (
    figure_id    INTEGER PRIMARY KEY,
    report_id    INTEGER NOT NULL REFERENCES documents(report_id),
    statement    TEXT NOT NULL,
    metric       TEXT NOT NULL,   -- our field name, e.g. net_sales
    tag          TEXT NOT NULL,   -- XBRL concept
    line_label   TEXT,
    period       TEXT NOT NULL,   -- 'Q2 2023' (3M) or '6M 2023' (YTD)
    period_start TEXT,
    period_end   TEXT,
    duration     TEXT NOT NULL,   -- '3M' | 'YTD'
    value        REAL NOT NULL,
    unit         TEXT NOT NULL,   -- 'USD' | 'USD/share'
    is_derived   INTEGER DEFAULT 0,
    page         INTEGER          -- best effort; NULL for HTML filings
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id  INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES documents(report_id),
    section   TEXT,
    heading   TEXT,
    ordinal   INTEGER,
    text      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_fig_lookup ON figures(metric, period, report_id);
CREATE INDEX IF NOT EXISTS ix_doc_company_period ON documents(company_id, period);
CREATE INDEX IF NOT EXISTS ix_chunks_report ON chunks(report_id);
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | str = DB_PATH, reset: bool = False) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if reset and p.exists():
        p.unlink()
    conn = connect(p)
    conn.executescript(SCHEMA)
    return conn
