"""Answer writer: the last LLM call turns the verified facts into a natural answer.

The structured paths find every number (SQL) and every reason (cited passages) first. The writer
only phrases them: it gets the question and the facts with their references, never filing text.
Code then checks its answer:
  * every number in it must occur in the facts or the question (no new or changed numbers),
  * every reference ([F1], [C123]) must be one of the facts' references, and there is at least one.
If a check fails, the template answer is kept and the reason is recorded. Turning the writer off
(FINQA_WRITER=off, or system A0 in the experiments) gives the template answer for the ablation.
"""
from __future__ import annotations

import os
import re

from . import llm

ENABLED = os.environ.get("FINQA_WRITER", "on").lower() != "off"

SYSTEM = """You write the answer to a question about a company's SEC filing, using ONLY the facts given.
- Answer the question directly in 2-5 sentences of plain prose (a short list is fine for several reasons).
- Copy every number exactly as written in the facts (no rounding, no new numbers, no own calculations).
  For a change, use the computed change from the facts.
- Put the reference of each fact you use in brackets right after it, e.g. [F1] or [C123].
- If there are assumptions or flags, state them in one short sentence at the end; otherwise say nothing about them.
- Do not comment on the facts, their sources or how they were computed, and do not add outside knowledge."""

_REF = re.compile(r"\[([FC]\d+)\]")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers(text: str) -> set[str]:
    """Numbers in a text, without references and thousands separators ('8,325' -> '8325')."""
    return {n.replace(",", "").rstrip(".") for n in _NUMBER.findall(_REF.sub(" ", text))}


def check(answer: str, facts: str, question: str) -> list[str]:
    """Problems with a written answer; empty if it only uses the facts."""
    problems = []
    new = numbers(answer) - numbers(facts) - numbers(question)
    if new:
        problems.append(f"numbers not in the facts: {sorted(new)}")
    refs, known = set(_REF.findall(answer)), set(_REF.findall(facts))
    if refs - known:
        problems.append(f"unknown references: {sorted(refs - known)}")
    if known and not refs:
        problems.append("no references")
    return problems


def write(question: str, facts: str, usage: llm.Usage, trace: dict) -> str | None:
    """The written answer, or None if it fails the checks (the caller keeps the template)."""
    try:
        answer = llm.text("write", SYSTEM, f"QUESTION:\n{question}\n\nFACTS:\n{facts}", usage, max_tokens=800).strip()
    except Exception as e:  # the template answer is still correct, so a failed writer is not an error
        trace["writer"] = {"used": False, "problems": [f"LLM call failed: {str(e)[:200]}"]}
        return None
    problems = check(answer, facts, question)
    trace["writer"] = {"used": not problems, "problems": problems}
    return None if problems else answer
