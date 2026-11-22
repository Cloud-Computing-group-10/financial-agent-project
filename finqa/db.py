"""Postgres database: schema, connection, and bulk loading (design plan 7 and 15.2).

One database holds everything the API needs:
  companies    one row per SEC filer (company_id = SEC CIK)
  documents    one row per 10-Q filing
  figures      numbers from the XBRL facts (layer 1)
  metric_tags  which XBRL tag each company uses for each metric, or why none fits
  chunks       10-Q text passages with their embedding vector (layer 2, pgvector)
"""
from __future__ import annotations

from typing import Iterable

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from .config import DATABASE_URL, EMBED_DIM

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS companies (
    company_id BIGINT PRIMARY KEY,      -- SEC CIK
    name       TEXT NOT NULL,
    ticker     TEXT,
    name_key   TEXT NOT NULL            -- normalised name for lookups, see facts.name_key()
);

CREATE TABLE IF NOT EXISTS documents (
    report_id  BIGSERIAL PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES companies,
    accession  TEXT NOT NULL,           -- a few 10-Qs are filed jointly by two companies
    form_type  TEXT NOT NULL,
    title      TEXT NOT NULL,
    filed      DATE NOT NULL,
    period     TEXT NOT NULL,           -- quarter covered, e.g. 'Q2 2023'
    period_end DATE NOT NULL,
    url        TEXT,
    file_path  TEXT,                    -- 10-Q document under data/, only if its text is indexed
    has_text   BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (company_id, accession)
);

CREATE TABLE IF NOT EXISTS figures (
    figure_id    BIGSERIAL PRIMARY KEY,
    report_id    BIGINT NOT NULL REFERENCES documents,
    statement    TEXT NOT NULL,
    metric       TEXT NOT NULL,         -- our field name, e.g. net_sales
    tag          TEXT NOT NULL,         -- XBRL concept
    line_label   TEXT,
    period       TEXT NOT NULL,         -- 'Q2 2023' (3M) or '6M 2023' (YTD)
    period_start DATE,
    period_end   DATE NOT NULL,
    duration     TEXT NOT NULL,         -- '3M' or 'YTD'
    value        DOUBLE PRECISION NOT NULL,
    unit         TEXT NOT NULL,         -- 'USD' or 'USD/share'
    is_derived   BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS metric_tags (
    company_id         BIGINT NOT NULL REFERENCES companies,
    metric             TEXT NOT NULL,
    tags               TEXT,            -- tags used, NULL if the metric is unsupported
    unsupported_reason TEXT,
    PRIMARY KEY (company_id, metric)
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id  BIGSERIAL PRIMARY KEY,
    report_id BIGINT NOT NULL REFERENCES documents,
    section   TEXT,
    heading   TEXT,
    ordinal   INTEGER,
    text      TEXT NOT NULL,
    embedding vector({EMBED_DIM}) NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_companies_ticker ON companies (lower(ticker));
CREATE INDEX IF NOT EXISTS ix_companies_name_key ON companies (name_key text_pattern_ops);
CREATE INDEX IF NOT EXISTS ix_documents_company_period ON documents (company_id, period);
CREATE INDEX IF NOT EXISTS ix_figures_lookup ON figures (metric, period, report_id);
CREATE INDEX IF NOT EXISTS ix_figures_report ON figures (report_id);
CREATE INDEX IF NOT EXISTS ix_chunks_report ON chunks (report_id);
"""

TABLES = ["chunks", "figures", "metric_tags", "documents", "companies"]  # drop order

# Staging tables for load_facts(): plain columns, no keys, dropped at commit.
# Their columns match the dicts produced by facts.extract_company().
STAGING = {
    "companies": "company_id BIGINT, name TEXT, ticker TEXT, name_key TEXT",
    "documents": "accession TEXT, company_id BIGINT, form_type TEXT, title TEXT, filed DATE, period TEXT, "
                 "period_end DATE, url TEXT",
    "figures": "company_id BIGINT, accession TEXT, metric TEXT, statement TEXT, tag TEXT, line_label TEXT, period TEXT, "
               "period_start DATE, period_end DATE, duration TEXT, value DOUBLE PRECISION, unit TEXT, "
               "is_derived BOOLEAN",
    "metric_tags": "company_id BIGINT, metric TEXT, tags TEXT, unsupported_reason TEXT",
}

# Move staged rows into the real tables, only for companies not in the database yet.
MERGE = [
    """CREATE TEMP TABLE new_companies ON COMMIT DROP AS
           SELECT DISTINCT company_id FROM stage_companies
           EXCEPT SELECT company_id FROM companies""",
    """INSERT INTO companies (company_id, name, ticker, name_key)
           SELECT DISTINCT ON (company_id) company_id, name, ticker, name_key FROM stage_companies
           WHERE company_id IN (SELECT company_id FROM new_companies)""",
    """INSERT INTO documents (company_id, accession, form_type, title, filed, period, period_end, url)
           SELECT company_id, accession, form_type, title, filed, period, period_end, url FROM stage_documents
           WHERE company_id IN (SELECT company_id FROM new_companies)""",
    """INSERT INTO figures (report_id, statement, metric, tag, line_label, period, period_start,
                            period_end, duration, value, unit, is_derived)
           SELECT d.report_id, s.statement, s.metric, s.tag, s.line_label, s.period, s.period_start,
                  s.period_end, s.duration, s.value, s.unit, s.is_derived
           FROM stage_figures s JOIN documents d USING (company_id, accession)
           WHERE d.company_id IN (SELECT company_id FROM new_companies)""",
    """INSERT INTO metric_tags (company_id, metric, tags, unsupported_reason)
           SELECT company_id, metric, tags, unsupported_reason FROM stage_metric_tags
           WHERE company_id IN (SELECT company_id FROM new_companies)""",
]


def connect(url: str = DATABASE_URL) -> psycopg.Connection:
    """Connection whose rows come back as dicts and that understands pgvector values."""
    conn = psycopg.connect(url, row_factory=dict_row)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    register_vector(conn)
    return conn


def init_db(conn: psycopg.Connection, reset: bool = False) -> None:
    if reset:
        conn.execute("DROP TABLE IF EXISTS " + ", ".join(TABLES) + " CASCADE")
    conn.execute(SCHEMA)
    conn.commit()


def load_facts(conn: psycopg.Connection, tables: dict[str, Iterable[dict]]) -> int:
    """Load companies, documents, figures and metric_tags (rows as dicts) into the database.

    Rows are copied into staging tables first (fast COPY), then moved with plain SQL (MERGE).
    Companies that are already in the database are skipped, so the bulk load does not duplicate
    the pilot companies. Returns the number of new companies."""
    for table, columns in STAGING.items():
        conn.execute(f"CREATE TEMP TABLE stage_{table} ({columns}) ON COMMIT DROP")
    for table, rows in tables.items():
        names = [c.split()[0] for c in STAGING[table].split(", ")]
        with conn.cursor().copy(f"COPY stage_{table} ({', '.join(names)}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row([row[name] for name in names])
    for statement in MERGE:
        conn.execute(statement)
    n = conn.execute("SELECT count(*) AS n FROM new_companies").fetchone()["n"]
    conn.commit()
    return n
