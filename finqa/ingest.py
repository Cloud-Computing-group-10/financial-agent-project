"""Pilot ingestion: the 5 pilot companies, including their 10-Q text for passage search.

  python -m finqa.ingest            download missing SEC files, then rebuild the database
  python -m finqa.ingest --offline  rebuild only from the files already in data/raw

Steps:
  1. download   companyfacts + submissions JSON and the 10-Q documents into data/raw
  2. figures    finqa.facts turns the XBRL facts into rows; db.load_facts() writes them
  3. text       each indexed 10-Q is split into passages, embedded, and stored in chunks

This resets the database. The numbers of every other SEC filer are added afterwards by the
Spark job (spark/facts_job.py) and `python -m finqa.load_bulk`.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from . import text as textmod
from .config import PILOT_COMPANIES, PILOT_YEARS, RAW_DIR, SEC_USER_AGENT, TEXT_YEARS
from .db import connect, init_db, load_facts
from .facts import extract_company, quarter_label
from .search import embed, passage_text

HEADERS = {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}


# ---------------------------------------------------------------------------- 1. download
def fetch(url: str, dest: Path, offline: bool) -> Path:
    """Download url to dest unless the file is already there."""
    if dest.exists() or offline:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    dest.write_bytes(r.content)
    time.sleep(0.15)  # SEC fair access: stay well under 10 requests per second
    return dest


def download(offline: bool) -> None:
    for c in PILOT_COMPANIES:
        cik, ticker = c["cik"], c["ticker"]
        fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
              RAW_DIR / f"companyfacts_{ticker}.json", offline)
        fetch(f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
              RAW_DIR / f"submissions_{ticker}.json", offline)
        for filing in text_filings(ticker):
            fetch(filing["url"], filing["path"], offline)


def text_filings(ticker: str) -> list[dict]:
    """The company's 10-Qs whose text we index (quarter ends in TEXT_YEARS)."""
    company = next(c for c in PILOT_COMPANIES if c["ticker"] == ticker)
    recent = json.loads((RAW_DIR / f"submissions_{ticker}.json").read_text())["filings"]["recent"]
    filings = []
    for form, accession, report_date, document in zip(
            recent["form"], recent["accessionNumber"], recent["reportDate"], recent["primaryDocument"]):
        if form != "10-Q" or int(quarter_label(report_date).split()[1]) not in TEXT_YEARS:
            continue
        filings.append({
            "accession": accession,
            "url": f"https://www.sec.gov/Archives/edgar/data/{company['cik']}/"
                   f"{accession.replace('-', '')}/{document}",
            "path": RAW_DIR / ticker / document,
        })
    return filings


# ---------------------------------------------------------------------------- 2. figures
def load_figures(conn) -> None:
    tables: dict[str, list[dict]] = {"companies": [], "documents": [], "figures": [], "metric_tags": []}
    for c in PILOT_COMPANIES:
        data = json.loads((RAW_DIR / f"companyfacts_{c['ticker']}.json").read_text())
        rows = extract_company(data, ticker=c["ticker"], years=PILOT_YEARS, name=c["name"])
        for table in tables:
            tables[table] += rows[table]
    load_facts(conn, tables)


# ---------------------------------------------------------------------------- 3. text
def load_text(conn, ticker: str) -> int:
    """Chunk, embed and store the text of the company's indexed 10-Qs. Returns the chunk count."""
    n = 0
    for filing in text_filings(ticker):
        if not filing["path"].exists():
            continue
        doc = conn.execute(
            """UPDATE documents SET url = %s, file_path = %s, has_text = TRUE
               WHERE accession = %s RETURNING report_id""",
            (filing["url"], str(filing["path"].relative_to(RAW_DIR.parent)), filing["accession"])).fetchone()
        if not doc:
            continue  # a filing without usable facts has no documents row

        chunks, seen = [], set()
        for ch in textmod.filing_chunks(filing["path"].read_text(errors="ignore")):
            if ch["text"] not in seen:  # highlights are often repeated word for word
                seen.add(ch["text"])
                chunks.append(ch)
        vectors = embed([passage_text(ch["heading"], ch["text"]) for ch in chunks])
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO chunks (report_id, section, heading, ordinal, text, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                [(doc["report_id"], ch["section"], ch["heading"], ch["ordinal"], ch["text"], vec)
                 for ch, vec in zip(chunks, vectors)])
        n += len(chunks)
    conn.commit()
    return n


def build(offline: bool) -> None:
    download(offline)
    with connect() as conn:
        init_db(conn, reset=True)
        load_figures(conn)
        n_chunks = sum(load_text(conn, c["ticker"]) for c in PILOT_COMPANIES)
        counts = conn.execute("""SELECT (SELECT count(*) FROM documents) AS documents,
                                        (SELECT count(*) FROM figures) AS figures""").fetchone()
        unsupported = conn.execute("""SELECT c.ticker, m.metric FROM metric_tags m JOIN companies c USING (company_id)
                                      WHERE m.tags IS NULL ORDER BY 1, 2""").fetchall()
    print(f"documents: {counts['documents']}  figures: {counts['figures']}  chunks: {n_chunks}")
    print("unsupported metrics:", ", ".join(f"{r['ticker']}/{r['metric']}" for r in unsupported))


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="do not download; use data/raw only")
    build(parser.parse_args().offline)
