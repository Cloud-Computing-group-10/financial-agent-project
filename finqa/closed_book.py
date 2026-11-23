"""Closed-book baseline (system N): the LLM gets only the question, no filing and no retrieval.

It answers "why not just ask the model?": anything it gets right comes from its training data, so
it fails on filings after its cutoff and cannot cite a filing it never saw. It uses the same
instruction as the RAG baseline (system C), minus the passages.
"""
from __future__ import annotations

import time

from . import llm

INSTRUCTION = """Answer the question about the company from your own knowledge of its SEC filings.
Cite the source of every number and every claim (filing, statement or section, period).
If you do not know the answer, or the question is ambiguous, say so instead of guessing."""


def closed_book_answer(question: str) -> dict:
    t0 = time.time()
    usage = llm.Usage()
    text = llm.text("closed_book", INSTRUCTION, f"Question: {question}", usage, max_tokens=2000)
    return {"question": question, "status": "answered", "answer": text, "llm": llm.model_name(),
            "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens, "llm_calls": usage.calls},
            "latency_s": round(time.time() - t0, 3)}
