"""Generous baseline (plan 12.1): a plain LLM given the full correct 10-Q, no tools or workflow,
with the same instruction to cite sources."""
from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path

from . import llm
from .config import DATA_DIR
from .db import connect
from .text import filing_plaintext

INSTRUCTION = """Answer the question about the company's results using only the SEC 10-Q filing provided.
Cite the source of every number and every explanatory claim (statement or section, line item, period).
If the filing does not contain enough evidence, or the question is ambiguous, say so instead of guessing."""


@lru_cache(maxsize=32)
def _doc_text(file_path: str) -> str:
    return filing_plaintext((DATA_DIR / file_path).read_text(errors="ignore"))  # stored relative to data/


def find_document(ticker: str | None, period: str | None) -> dict | None:
    conn = connect()
    if ticker and period:
        row = conn.execute("""SELECT d.* FROM documents d JOIN companies c USING(company_id)
                              WHERE c.ticker=? AND d.period=? AND d.has_text=1""", (ticker, period)).fetchone()
    elif ticker:
        row = conn.execute("""SELECT d.* FROM documents d JOIN companies c USING(company_id)
                              WHERE c.ticker=? AND d.has_text=1 ORDER BY d.period_end DESC LIMIT 1""", (ticker,)).fetchone()
    else:
        row = None
    conn.close()
    return dict(row) if row else None


def baseline_answer(question: str, ticker: str | None = None, period: str | None = None) -> dict:
    """ticker/period select the 'correct' document (from the answer key). Without them the
    baseline infers the company with the rule parser (still generous: it gets the right filing)."""
    t0 = time.time()
    if ticker is None:  # CLI use: pick the filing from the names in the question
        from .parse import find_company, find_period
        ticker, period = find_company(question), find_period(question)
        period = None if period == "latest" else period
    doc = find_document(ticker, period) if ticker else None
    usage = llm.Usage()
    if not llm.available():
        return {"question": question, "status": "skipped", "answer": f"[baseline skipped: {llm.model_name()} not available]",
                "usage": {}, "latency_s": 0}
    if doc:
        context = f"FILING: {doc['title']} (accession {doc['accession']})\n\n{_doc_text(doc['file_path'])}"
    else:
        context = "FILING: (no filing available for this company/period)"
    # Small local models cannot hold a whole 10-Q. Do not truncate silently (that would be an
    # unfair baseline); report it as a context-window failure instead (plan 12.5 limitation).
    limit = llm.context_limit()
    est_tokens = len(context) // 4
    if limit and est_tokens > limit - 4000:
        return {"question": question, "status": "context_overflow", "llm": llm.model_name(),
                "answer": f"[baseline not run: filing is ~{est_tokens} tokens, over the {limit}-token context of {llm.model_name()}]",
                "document": doc and doc["accession"], "usage": {}, "latency_s": 0}
    text = llm.text("baseline", INSTRUCTION, f"{context}\n\nQUESTION: {question}", usage, max_tokens=4000)
    return {"question": question, "status": "answered", "answer": text, "document": doc and doc["accession"],
            "llm": llm.model_name(),
            "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens, "llm_calls": usage.calls},
            "latency_s": round(time.time() - t0, 3)}
