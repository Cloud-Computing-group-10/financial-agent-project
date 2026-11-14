"""Step 1: parse the question into company / metric / period / type with the LLM.

The LLM proposes a structured parse (validated against the Pydantic schema). Code then checks it:
  * values must come from the allowed lists (companies table, metric list),
  * the company must actually be named in the question ("ask, don't guess"),
  * scope rules (10-K/annual, Q4, ratios, segments) are deterministic and always apply,
  * a question without a "why" word is never routed to the explanation path.
These checks are validation, not a fallback parser: if the LLM call fails, the question fails.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, field_validator

from . import llm
from .config import COMPANIES, METRICS

QType = Literal["numeric", "explanatory", "both"]
Compare = Literal["none", "yoy", "qoq"]


class ParsedQuestion(BaseModel):
    # Field meanings are explained once in the system prompt (no per-field descriptions here,
    # they would be sent a second time inside the schema and cost input tokens).
    company: Optional[str] = None
    metric: Optional[str] = None
    period: Optional[str] = None
    qtype: QType
    compare: Compare = "none"
    unsupported_reason: Optional[str] = None

    @field_validator("compare", mode="before")
    @classmethod
    def _none_is_none(cls, v):  # small local models often emit null instead of "none"
        return "none" if v in (None, "", "null") else v


_SYSTEM = """Convert a question about a company's quarterly results into JSON. Data: SEC 10-Q filings only.
company: ticker, one of {companies}; null if no company is named. Never guess.
metric: one of {metrics}; null if none fits.
period: "Q<n> <yyyy>"; "latest" for last/most recent quarter; null if not stated.
qtype: numeric (what/how much), explanatory (why/how), both (amount AND reason).
compare: yoy (vs same quarter a year earlier, or plain "change/growth"), qoq (vs previous quarter), none.
unsupported_reason: short reason if it asks for annual/10-K or Q4 data, a ratio/margin %, or segment numbers; else null."""


def _system_prompt() -> str:
    return _SYSTEM.format(
        companies=", ".join(f"{c['ticker']} ({c['name']})" for c in COMPANIES),
        metrics=", ".join(f"{m} ({s['label'].lower()})" for m, s in METRICS.items()))


# ---------------------------------------------------------------------------- deterministic checks
_ORD = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}
_WHY = re.compile(r"\b(why|how come|what (drove|caused|explains|held back|hurt|helped|is behind)|held back|reason|"
                  r"driver|drivers|due to|explain|contribut|factors)", re.I)


def find_company(q: str) -> str | None:
    """Company named in the question (by name, ticker or common alias), or None."""
    ql = q.lower()
    for c in COMPANIES:
        for a in sorted(c["aliases"], key=len, reverse=True):
            if len(a) <= 3:
                if re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9&])", ql) and \
                        (a != "cat" or "caterpillar" in ql or re.search(r"\bCAT\b", q)):
                    return c["ticker"]
            elif a in ql:
                return c["ticker"]
    return None


def find_period(q: str) -> str | None:
    """Normalise a period mention ('Q2 2023', 'second quarter of 2023', 'last quarter')."""
    ql = q.lower()
    m = re.search(r"\bq([1-4])\s*(?:fy)?\s*'?(20\d\d|\d\d)\b", ql) or re.search(r"\b(20\d\d)\s*q([1-4])\b", ql)
    if m:
        a, b = m.groups()
        if a.startswith("20") and len(a) == 4:  # "2023 q2" form
            a, b = b, a
        return f"Q{a} {b if len(b) == 4 else '20' + b}"
    m = re.search(r"\b(first|second|third|fourth|1st|2nd|3rd|4th) quarter(?: of)?(?: fiscal)? (20\d\d)\b", ql)
    if m:
        return f"Q{_ORD[m.group(1)]} {m.group(2)}"
    if re.search(r"\b(last|latest|most recent|recent|this|current) quarter\b", ql):
        return "latest"
    return None


def scope_violation(q: str) -> str | None:
    """Out-of-scope questions, decided by code rather than by the model."""
    ql = q.lower()
    period = find_period(q)
    if re.search(r"\b(10-k|annual|full[- ]year|fiscal year)\b", ql):
        return "Annual (10-K) figures are out of scope; only quarterly 10-Q data is loaded."
    if period and period.startswith("Q4"):
        return "There is no Q4 10-Q (Q4 is only reported in the annual 10-K), which is out of scope."
    if re.search(r"\b(margin|ratio|dpo|dso|roe|roa|return on|turnover ratio|per employee)\b", ql):
        return "Derived or ratio metrics are out of scope in version 1."
    if re.search(r"\bsegment|division|by region|in (europe|asia|china|latin america|north america)\b", ql) \
            and not _WHY.search(q):
        return "Segment-level figures are not stored in version 1 (consolidated figures only)."
    return None


# ---------------------------------------------------------------------------- entry point
def parse_question(q: str, usage: llm.Usage) -> ParsedQuestion:
    p = llm.parse("parse", _system_prompt(), q, ParsedQuestion, usage, max_tokens=1000)

    named = find_company(q)
    if p.company != named:
        if named is None:
            usage.log.append({"step": "parse", "note": f"dropped LLM company guess {p.company!r} (not named in question)"})
        p.company = named
    if p.metric not in METRICS:
        p.metric = None
    if p.period and p.period != "latest" and not re.fullmatch(r"Q[1-4] 20\d\d", p.period):
        p.period = find_period(p.period)
    if not p.unsupported_reason:
        p.unsupported_reason = scope_violation(q)
    if p.qtype != "numeric" and not _WHY.search(q):
        p.qtype = "numeric"  # e.g. "how did sales change?" asks for numbers, not reasons
    return p
