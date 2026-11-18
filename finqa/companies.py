"""Find the company a question is about, in the companies table (every SEC filer we loaded).

The LLM only copies the company mention from the question ("Coca-Cola", "CAT", "J&J"). This
module decides which filer that is, so the LLM can never pick a company that is not in the data.
"""
from __future__ import annotations

from .config import COMPANY_ALIASES
from .facts import name_key

MAX_CANDIDATES = 5

SQL_BY_TICKER = "SELECT * FROM companies WHERE lower(ticker) = lower(%(ticker)s)"
SQL_BY_NAME = "SELECT * FROM companies WHERE name_key = %(key)s"
# A name that starts with the mention, e.g. 'honeywell' -> 'honeywell international'.
# Filers with a ticker and with more filings come first.
SQL_BY_NAME_PREFIX = """
SELECT c.*, count(d.report_id) AS n_filings
FROM companies c LEFT JOIN documents d USING (company_id)
WHERE c.name_key LIKE %(prefix)s
GROUP BY c.company_id
ORDER BY (c.ticker IS NULL), count(d.report_id) DESC, c.name
LIMIT %(limit)s
"""


def find_companies(conn, mention: str) -> list[dict]:
    """Companies matching a mention, best first. One result means the company is clear;
    several mean the question is ambiguous; none means the company is not in the data."""
    mention = mention.strip()
    ticker = COMPANY_ALIASES.get(mention.lower(), mention)
    rows = conn.execute(SQL_BY_TICKER, {"ticker": ticker}).fetchall()
    if rows:
        return rows

    key = name_key(mention)
    if not key:
        return []
    rows = conn.execute(SQL_BY_NAME, {"key": key}).fetchall()
    if rows:
        return rows
    rows = conn.execute(SQL_BY_NAME_PREFIX, {"prefix": key + " %", "limit": MAX_CANDIDATES}).fetchall()
    # One filer with a ticker beats unlisted namesakes (old shells, subsidiaries).
    listed = [r for r in rows if r["ticker"]]
    return listed[:1] if len(listed) == 1 else rows


def company_by_ticker(conn, ticker: str) -> dict | None:
    return conn.execute(SQL_BY_TICKER, {"ticker": ticker}).fetchone()
