"""Passage search over 10-Q text chunks: BM25 (keyword), semantic (embeddings), hybrid (RRF).

The three methods rank the same candidate passages, so the search experiment (plan 16.6) changes
only the ranking. Scores come from the search itself; the workflow's confidence is based on these
scores, never on the LLM's own opinion (plan section 9).
"""
from __future__ import annotations

import re

from rank_bm25 import BM25Okapi

from .config import MDA_SECTION
from .embed import embed, passage_text
from .timing import timed

METHODS = ("semantic", "bm25", "hybrid")
MDA = MDA_SECTION

_TOKEN = re.compile(r"[a-z0-9]+(?:[.'’-][a-z0-9]+)*")
STOP = set("""a an the of to in on for and or by with is are was were be been this that these those
it its as at from than which what why how did does do our we us their they during period quarter
compared versus vs""".split())

# Candidate passages of the given filings, with their cosine similarity to the query (pgvector).
SQL_CANDIDATES = """
SELECT c.chunk_id, c.report_id, c.section, c.heading, c.ordinal, c.text,
       d.accession, d.title, d.period, d.url,
       1 - (c.embedding <=> %(query_vector)s) AS similarity
FROM chunks c JOIN documents d USING (report_id)
WHERE c.report_id = ANY(%(report_ids)s) AND c.section ILIKE %(section)s
ORDER BY c.chunk_id
"""


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOP]


def search(conn, query: str, report_ids: list[int], method: str = "semantic", k: int = 5,
           section: str | None = None, trace: dict | None = None) -> list[dict]:
    """Top-k passages from the given filings, each with its score and source metadata.

    section: optional filter on the section label, e.g. MDA. trace: gets the step timings."""
    trace = trace if trace is not None else {}
    with timed(trace, "search_embed"):
        query_vector = embed([query])[0]
    with timed(trace, "search_db"):
        rows = conn.execute(SQL_CANDIDATES, {
            "query_vector": query_vector,
            "report_ids": report_ids,
            "section": f"%{section}%" if section else "%",
        }).fetchall()
    if not rows:
        return []

    with timed(trace, "search_rank"):
        if method == "semantic":
            scores = [r["similarity"] for r in rows]
        elif method == "bm25":
            scores = _bm25_scores(query, rows)
        elif method == "hybrid":
            scores = _rrf_scores([r["similarity"] for r in rows], _bm25_scores(query, rows))
        else:
            raise ValueError(f"unknown search method {method!r}, choose from {METHODS}")
        ranked = sorted(zip(rows, scores), key=lambda pair: -pair[1])[:k]
    return [{**row, "score": round(float(score), 4), "method": method, "locator": locator(row)}
            for row, score in ranked]


def locator(row: dict) -> str:
    heading = f" › {row['heading']}" if row["heading"] else ""
    return f"{row['title']} › {row['section']}{heading} (chunk {row['chunk_id']})"


def _bm25_scores(query: str, rows: list[dict]) -> list[float]:
    bm25 = BM25Okapi([tokenize(passage_text(r["heading"], r["text"])) for r in rows])
    return bm25.get_scores(tokenize(query)).tolist()


def _rrf_scores(*score_lists: list[float], k: int = 60) -> list[float]:
    """Reciprocal-rank fusion: each list adds 1 / (k + rank) for every passage."""
    fused = [0.0] * len(score_lists[0])
    for scores in score_lists:
        order = sorted(range(len(scores)), key=lambda i: -scores[i])
        for rank, i in enumerate(order):
            fused[i] += 1.0 / (k + rank + 1)
    return fused
