"""Simple RAG baseline (plan 16.2, system C): the usual approach most people would build.

The top passages of the correct filing (same index and search as the workflow's document QA
path) plus the question go to the LLM. No parsing, no SQL, no routing, no code-based arithmetic,
no citation checks and no decline rules: this isolates what the workflow around retrieval adds.
"""
from __future__ import annotations

import time

from . import llm
from .db import connect
from .docqa import TOP_K
from .search import search

INSTRUCTION = """Answer the question about the company using only the passages provided from its SEC filing.
Cite the passage ids ([C<id>]) for every number and every claim.
If the passages do not contain enough evidence, or the question is ambiguous, say so instead of guessing."""


def rag_answer(question: str, accession: str, method: str = "hybrid", k: int = TOP_K) -> dict:
    """accession selects the correct filing (from the answer key), like the full-document baseline."""
    t0 = time.time()
    usage = llm.Usage()
    with connect() as conn:
        doc = conn.execute("SELECT report_id FROM documents WHERE accession = %s", (accession,)).fetchone()
        passages = search(conn, question, [doc["report_id"]], method=method, k=k) if doc else []
    if not passages:
        return {"question": question, "status": "abstain", "answer": "No passages found.", "document": accession,
                "usage": {}, "latency_s": round(time.time() - t0, 3)}
    context = "\n\n".join(f"[C{p['chunk_id']}] ({p['section']} › {p['heading']})\n{p['text']}" for p in passages)
    text = llm.text("rag", INSTRUCTION, f"Question: {question}\n\nPassages:\n{context}", usage, max_tokens=2000)
    return {"question": question, "status": "answered", "answer": text, "document": accession,
            "passages": [p["chunk_id"] for p in passages], "llm": llm.model_name(),
            "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens, "llm_calls": usage.calls},
            "latency_s": round(time.time() - t0, 3)}
