"""LLM grader for free-text answers ("why" questions, FinanceBench, declines of the RAG baseline).

One fixed grader model for every system and model under test, so grades are comparable. Plan 16.5:
the grader drafts the grades; a person checks a sample and the report states the agreement.
"""
from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel

JUDGE_MODEL = os.environ.get("FINQA_JUDGE_MODEL", "claude-haiku-5-5")  # cheap; agreement checked with a stronger model on a sample

SYSTEM = """You grade an answer to a question about a company's SEC filing against a reference.
- correct: the answer states the reference's key facts or reasons (wording may differ; numbers may be
  rounded or in other units as long as they agree, e.g. 1,577 million = 1.58 billion). Extra true
  details are fine. Missing a minor secondary point is still correct; missing the main point is not.
- incorrect: wrong numbers, wrong or missing main reason, or a contradiction of the reference.
- declined: the answer does not answer (says the information is not available, asks to clarify,
  or refuses). If the reference itself says the question must be declined, an answer that declines is
  graded "declined" and one that gives an answer is graded "incorrect"."""


class Verdict(BaseModel):
    verdict: Literal["correct", "incorrect", "declined"]
    reason: str


class Judge:
    def __init__(self, model: str = JUDGE_MODEL):
        import anthropic
        self.model = model
        self.client = anthropic.Anthropic(timeout=120, max_retries=3)

    def grade(self, question: str, reference: str, answer: str) -> dict:
        """The verdict. A reply without a parsed verdict (cut off, or a refusal) is retried with more
        room; after three tries it raises, and the answer stays ungraded."""
        for max_tokens in (1000, 4000, 8000):
            resp = self.client.messages.parse(
                model=self.model, max_tokens=max_tokens, system=SYSTEM,
                messages=[{"role": "user", "content": f"QUESTION:\n{question}\n\nREFERENCE:\n{reference}\n\nANSWER:\n{answer}"}],
                output_format=Verdict, output_config={"effort": "low"})
            if resp.parsed_output is not None:
                return resp.parsed_output.model_dump()
        raise RuntimeError(f"no verdict from the grader (stop reason: {resp.stop_reason})")
