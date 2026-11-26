"""Document QA path: questions the structured data cannot answer (ratios, 10-K items, text questions).

  passages of one filing (hybrid search) -> LLM: answer + cited claims, and for a calculation the
  inputs (each with the passage it is printed in) plus a formula -> code checks the citations,
  checks that each input number appears in its passage, and computes the formula.

The same rules as the rest of the workflow: the LLM never does arithmetic, every claim must cite
a passage we retrieved, and the system declines when the passages do not answer the question.
"""
from __future__ import annotations

import ast
import itertools
import operator
import re
from typing import Optional

from pydantic import BaseModel, Field

from . import llm
from .search import locator, search
from .timing import timed

TOP_K = 10  # statements are spread over several page pieces, so more passages than for "why" questions

# Version 2: the three primary financial statements are always given to the LLM, next to the search
# results. Search alone often missed them: a statement page is mostly numbers, so it matches a
# question's words poorly. Titles are matched with all whitespace removed, because PDF text often
# breaks words ("Balance Shee\nt"). A statement page also has many numbers, and the three statements
# sit within a few pages of each other, which rules out tables of contents and the MD&A.
STATEMENT_TITLE = re.compile(r"(?<!comprehensive)(balancesheet|statements?of(?:consolidated)?"
                             r"(?:financialposition|operations|income|earnings|cashflow)|incomestatement|cashflowsstatement)")
BIG_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+|\d{3,}")
MIN_NUMBERS = 25     # numbers on a statement page
MAX_SPAN = 8         # the statements lie within this many pages (or sections) of each other

SQL_FILING_PASSAGES = """
SELECT c.chunk_id, c.report_id, c.section, c.heading, c.ordinal, c.text, d.accession, d.title, d.period, d.url
FROM chunks c JOIN documents d USING (report_id)
WHERE c.report_id = %(report_id)s ORDER BY c.ordinal
"""


class Claim(BaseModel):
    text: str = Field(description="One statement from the passages")
    chunk_ids: list[int] = Field(description="IDs of the supporting passages (the number in [C<id>])")


class Input(BaseModel):
    name: str = Field(description="Short variable name, e.g. ap_2017")
    value: float = Field(description="The number exactly as printed in the passage (same units, no scaling)")
    chunk_id: int = Field(description="ID of the passage that prints this number")


class DocAnswer(BaseModel):
    answerable: bool = Field(description="False if the passages do not contain what is needed")
    answer: str = Field(description="The answer in one or two sentences; write {result} where the computed value goes")
    claims: list[Claim]
    inputs: list[Input] = Field(default_factory=list, description="Numbers needed for a calculation")
    formula: Optional[str] = Field(default=None, description="Arithmetic over the input names, e.g. (a + b) / 2 / c * 365")


SYSTEM = """You answer a question about a company using ONLY the passages provided from its SEC filing.
Every claim must cite the passage ids it comes from. Do not use outside knowledge.
Never compute numbers yourself. If the answer needs a calculation, list each input number exactly as
printed in a passage (same units, no scaling) with the passage id, give a formula over the input names
using + - * / ( ), and write {result} in the answer where the computed value goes; state the unit
(e.g. "USD millions", "%", "times"). Multiply by 100 in the formula if a percentage is asked.
If the passages do not contain what is needed, set answerable=false."""


# ---------------------------------------------------------------------------- arithmetic
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}


def evaluate(formula: str, values: dict[str, float]) -> float:
    """Arithmetic only: numbers, the given names, + - * / ** and parentheses."""
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id in values:
            return values[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError(f"not allowed in a formula: {ast.dump(node)[:60]}")
    return ev(ast.parse(formula, mode="eval"))


def printed_in(value: float, text: str) -> bool:
    """True if the number is printed in the text, e.g. 1577 as '1,577' or '$1,577.0' or '(1,577)'."""
    digits = re.sub(r"(?<=\d),(?=\d{3})|\$", "", text)  # drop thousands separators and $, keep line breaks
    a = abs(value)
    forms = {f"{a:.0f}", f"{a:.1f}", f"{a:.2f}", f"{a:g}"}
    forms = {f for f in forms if float(f) == a or abs(float(f) - a) < 1e-9}
    return any(re.search(rf"(?<![\d.]){re.escape(f)}(?!\.?\d)", digits) for f in forms)


def fmt_result(x: float) -> str:
    return f"{x:,.2f}" if abs(x) < 1e6 else f"{x:,.0f}"


# ---------------------------------------------------------------------------- statements
def statement_kind(head: str) -> str | None:
    """'balance', 'income' or 'cash' if the start of a page is a statement title, else None."""
    m = STATEMENT_TITLE.search(re.sub(r"\s+", "", head).lower())
    if not m:
        return None
    t = m.group(1)
    return "balance" if ("balance" in t or "position" in t) else "cash" if "cash" in t else "income"


def find_statements(groups: list[list[dict]]) -> list[int]:
    """Indexes of the groups (pages or sections, in document order) that hold the balance sheet, the
    income statement and the cash-flow statement: the earliest set that lies within MAX_SPAN groups."""
    candidates: dict[str, list[int]] = {}
    for i, pieces in enumerate(groups):
        kind = statement_kind(pieces[0]["text"][:300])
        if kind and len(BIG_NUMBER.findall(" ".join(p["text"] for p in pieces))) >= MIN_NUMBERS:
            candidates.setdefault(kind, []).append(i)
    if not candidates:
        return []
    sets = [s for s in itertools.product(*candidates.values()) if max(s) - min(s) <= MAX_SPAN]
    return sorted(min(sets, key=min)) if sets else []


def statement_passages(conn, report_id: int) -> list[dict]:
    """Every passage of the filing's three primary statements (empty if they are not found)."""
    groups: dict[str, list[dict]] = {}
    for row in conn.execute(SQL_FILING_PASSAGES, {"report_id": report_id}).fetchall():
        groups.setdefault(row["section"], []).append(row)  # dicts keep the document order
    ordered = list(groups.values())
    return [{**p, "score": None, "method": "statement", "locator": locator(p)}
            for i in find_statements(ordered) for p in ordered[i]]


# ---------------------------------------------------------------------------- path
def answer_from_document(conn, question: str, report_id: int, usage: llm.Usage, trace: dict,
                         method: str = "hybrid", search_terms: str | None = None,
                         statements: bool = True) -> dict:
    """{"status", "text", "citations", "flags"} for one filing. search_terms (from the parse step)
    are the words a filing uses; long questions with instructions retrieve poorly on their own.
    statements: also give the three primary financial statements (version 2; False is version 1)."""
    query = f"{search_terms}\n{question}" if search_terms else question
    passages = search(conn, query, [report_id], method=method, k=TOP_K, trace=trace)
    trace["docqa"] = {"report_id": report_id, "method": method, "query": query[:300],
                      "top_scores": [p["score"] for p in passages]}
    if statements:
        with timed(trace, "sql_statements"):
            seen = {p["chunk_id"] for p in passages}
            extra = [p for p in statement_passages(conn, report_id) if p["chunk_id"] not in seen]
        passages += extra
        trace["docqa"]["statement_passages"] = len(extra)
    if not passages:
        return {"status": "abstain", "text": "Not enough evidence: the filing has no indexed text.", "citations": [], "flags": []}

    by_id = {p["chunk_id"]: p for p in passages}
    context = "\n\n".join(f"[C{p['chunk_id']}] ({p['section']} › {p['heading']})\n{p['text']}" for p in passages)
    with timed(trace, "llm_answer"):
        out = llm.parse("docqa", SYSTEM, f"Question: {question}\n\nPassages:\n{context}", DocAnswer, usage, max_tokens=3000)
    if not out.answerable:
        return {"status": "abstain", "text": "The filing passages do not answer this question.", "citations": [], "flags": []}

    flags = []
    claims = [{"text": c.text, "chunk_ids": [i for i in c.chunk_ids if i in by_id]} for c in out.claims]
    claims = [c for c in claims if c["chunk_ids"]]  # citation existence check
    text = out.answer
    if out.formula:
        values = {i.name: i.value for i in out.inputs}
        for i in out.inputs:  # each input must be printed in a retrieved passage (the cited one, or else
            if i.chunk_id in by_id and printed_in(i.value, by_id[i.chunk_id]["text"]):  # the one that prints it)
                continue
            found = next((p["chunk_id"] for p in passages if printed_in(i.value, p["text"])), None)
            if found:
                i.chunk_id = found
            else:
                flags.append(f"Input {i.name}={i.value:g} is not printed in any retrieved passage.")
        try:
            result = evaluate(out.formula, values)
        except (ValueError, SyntaxError, ZeroDivisionError) as e:
            return {"status": "abstain", "text": f"Could not compute the answer ({e}).", "citations": [], "flags": flags}
        text = text.replace("{result}", fmt_result(result)) if "{result}" in text else f"{text} Result: {fmt_result(result)}."
        trace["docqa"].update({"formula": out.formula, "inputs": values, "result": result})
    elif not claims:
        return {"status": "abstain", "text": "The answer could not be tied to a retrieved passage.", "citations": [], "flags": []}

    used = {i for c in claims for i in c["chunk_ids"]} | {i.chunk_id for i in out.inputs if i.chunk_id in by_id}
    lines = [text] + [f"- {c['text']} " + "".join(f"[C{i}]" for i in c["chunk_ids"]) for c in claims]
    if out.formula:
        lines.append("Computed: " + out.formula + " with " +
                     ", ".join(f"{i.name}={i.value:g} [C{i.chunk_id}]" for i in out.inputs))
    citations = [{"ref": f"C{p['chunk_id']}", "locator": p["locator"], "score": p["score"], "url": p["url"],
                  "text": p["text"][:500]} for p in passages if p["chunk_id"] in used]
    trace["docqa"]["inputs_printed"] = not flags
    return {"status": "answered", "text": "\n".join(lines), "citations": citations, "flags": flags}
