"""The 5-step workflow: parse -> route -> retrieve/compute -> check -> cited answer or abstain.

Design rule: the LLM handles language; numbers come from fixed SQL templates over the figures
table, and source tags come from the database/search metadata, never from the model.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from pydantic import BaseModel, Field

from . import llm
from .config import COMPANIES, MAX_ROUNDS, METRICS, MIN_PASSAGES, THRESHOLDS, TOP_K
from .db import connect
from .parse import ParsedQuestion, parse_question
from .search import MDA, search

# ============================================================================ SQL templates
# The LLM never writes SQL; it only supplies (ticker, metric, period) which code validates.
SQL_LOOKUP = """
SELECT f.*, d.title, d.accession, d.url, d.period AS doc_period, d.date AS filed
FROM figures f JOIN documents d USING(report_id) JOIN companies c USING(company_id)
WHERE c.ticker = :ticker AND f.metric = :metric AND f.period = :period AND f.duration = '3M'
ORDER BY (d.period = f.period) DESC, d.date ASC
"""

SQL_CHANGE = """
SELECT cur.value AS cur_value, prev.value AS prev_value,
       cur.value - prev.value AS abs_change,
       CASE WHEN prev.value = 0 THEN NULL
            ELSE ROUND((cur.value - prev.value) * 100.0 / ABS(prev.value), 2) END AS pct_change
FROM figures cur, figures prev
WHERE cur.figure_id = :cur_id AND prev.figure_id = :prev_id
"""

SQL_LATEST_DOC = """
SELECT d.* FROM documents d JOIN companies c USING(company_id)
WHERE c.ticker = :ticker {text_filter}
ORDER BY d.period_end DESC LIMIT 1
"""

SQL_DOC_FOR_PERIOD = """
SELECT d.* FROM documents d JOIN companies c USING(company_id)
WHERE c.ticker = :ticker AND d.period = :period
"""


# ============================================================================ helpers
def fmt_value(v: float, unit: str) -> str:
    if unit == "USD/share":
        return f"${v:,.2f} per share"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.3f} billion"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.0f} million"
    return f"{sign}${a:,.0f}"


def shift_period(period: str, compare: str) -> str:
    q, y = int(period[1]), int(period[3:])
    if compare == "yoy":
        return f"Q{q} {y - 1}"
    return f"Q4 {y - 1}" if q == 1 else f"Q{q - 1} {y}"


def figure_locator(f: dict) -> str:
    derived = ", derived as YTD difference" if f["is_derived"] else ""
    return (f"{f['title']} ({f['accession']}) › {f['statement']} › {f['line_label']} "
            f"[us-gaap:{f['tag']}] › {f['period']} (three months{derived})")


def company_name(ticker: str) -> str:
    return next(c["name"] for c in COMPANIES if c["ticker"] == ticker)


# ============================================================================ numeric path
def lookup(conn, ticker: str, metric: str, period: str) -> tuple[Optional[dict], list[dict]]:
    """Return (primary figure, conflicting figures). Primary = filing that covers the period."""
    rows = [dict(r) for r in conn.execute(SQL_LOOKUP, {"ticker": ticker, "metric": metric, "period": period})]
    if not rows:
        return None, []
    primary = rows[0]
    conflicts = [r for r in rows[1:] if abs(r["value"] - primary["value"]) > 0.5 * (10 ** -6) * max(1, abs(primary["value"]))
                 and r["value"] != primary["value"]]
    return primary, conflicts


def _unsupported_reason(ticker: str, metric: str) -> str | None:
    """From the pre-ingest alignment check: why a metric has no tag for this company."""
    import json
    from .config import ALIGNMENT_REPORT
    try:
        entry = json.loads(ALIGNMENT_REPORT.read_text())[ticker][metric]
    except (OSError, KeyError, ValueError):
        return None
    if entry.get("tag"):
        return None
    if entry.get("excluded"):
        return "; ".join(entry["excluded"].values())
    return "none of the XBRL tags " + ", ".join(entry.get("tried", [])) + " is reported"


def numeric_path(conn, p: ParsedQuestion, period: str, trace: dict) -> dict:
    out: dict = {"figures": [], "flags": []}
    cur, conflicts = lookup(conn, p.company, p.metric, period)
    trace["sql"] = [{"template": "SQL_LOOKUP", "params": [p.company, p.metric, period], "rows": int(cur is not None) + len(conflicts)}]
    if not cur:
        why = _unsupported_reason(p.company, p.metric)
        out["missing"] = (f"{company_name(p.company)} does not report {METRICS[p.metric]['label'].lower()} "
                          f"in its 10-Q data ({why})." if why else
                          f"No {METRICS[p.metric]['label']} figure for {p.company} in {period}.")
        return out
    out["figures"].append(cur)
    for c in conflicts:
        out["flags"].append(f"Conflict: {figure_locator(c)} reports {fmt_value(c['value'], c['unit'])} "
                            f"vs primary {fmt_value(cur['value'], cur['unit'])} (restatement?)")
    if p.compare in ("yoy", "qoq"):
        prev_period = shift_period(period, p.compare)
        prev, prev_conf = lookup(conn, p.company, p.metric, prev_period)
        trace["sql"].append({"template": "SQL_LOOKUP", "params": [p.company, p.metric, prev_period], "rows": int(prev is not None)})
        if not prev:
            reason = " (Q4 is only in the 10-K, out of scope)" if prev_period.startswith("Q4") else ""
            out["missing"] = f"No comparison figure for {prev_period}{reason}."
            return out
        out["figures"].append(prev)
        for c in prev_conf:
            out["flags"].append(f"Conflict: {figure_locator(c)} reports {fmt_value(c['value'], c['unit'])} "
                                f"vs primary {fmt_value(prev['value'], prev['unit'])} (restatement?)")
        ch = dict(conn.execute(SQL_CHANGE, {"cur_id": cur["figure_id"], "prev_id": prev["figure_id"]}).fetchone())
        trace["sql"].append({"template": "SQL_CHANGE", "params": [cur["figure_id"], prev["figure_id"]]})
        out["change"] = {**ch, "from": prev_period, "to": period, "compare": p.compare}
    return out


def render_numeric(p: ParsedQuestion, num: dict) -> str:
    cur = num["figures"][0]
    label = METRICS[p.metric]["label"]
    s = f"{company_name(p.company)}'s {label.lower()} for {cur['period']} was {fmt_value(cur['value'], cur['unit'])} [F1]."
    if "change" in num:
        ch, prev = num["change"], num["figures"][1]
        direction = "up" if ch["abs_change"] > 0 else "down" if ch["abs_change"] < 0 else "unchanged"
        pct = f"{abs(ch['pct_change']):.1f}%" if ch["pct_change"] is not None else "n/a"
        s += (f" That is {direction} {pct} ({fmt_value(abs(ch['abs_change']), cur['unit'])}) from "
              f"{fmt_value(prev['value'], prev['unit'])} in {prev['period']} [F2]"
              f" ({'year-over-year' if ch['compare'] == 'yoy' else 'quarter-over-quarter'}; computed in SQL).")
    return s


# ============================================================================ explanatory path
class Claim(BaseModel):
    text: str = Field(description="One explanatory claim, paraphrased from the passages")
    chunk_ids: list[int] = Field(description="IDs of the passages (the number in [C<id>]) supporting this claim")


class Explanation(BaseModel):
    answerable: bool = Field(description="False if the passages do not actually explain what was asked")
    claims: list[Claim]


_EXPLAIN_SYSTEM = """You explain changes in a company's results using ONLY the passages provided from
its SEC 10-Q filing. Every claim must cite the passage ids it comes from. Do not use outside knowledge.
Do not state any number that is not written in a cited passage or in the provided figures. If the
passages do not explain what is asked, set answerable=false and return no claims. Keep 2-5 claims."""


def _rewrite_query(q: str, p: ParsedQuestion) -> str:
    metric = METRICS[p.metric]["label"] if p.metric else ""
    return f"{metric} {q} increase decrease primarily due to driven by compared with prior year".strip()


def retrieve(q: str, p: ParsedQuestion, report_id: int, method: str, trace: dict) -> tuple[list[dict], bool]:
    """Search MD&A first (round 1), then the whole report with a rewritten query (round 2)."""
    th = THRESHOLDS[method]
    query = q
    passages: list[dict] = []
    for rnd in range(1, MAX_ROUNDS + 1):
        section = MDA if rnd == 1 else None
        hits = search(query, [report_id], method=method, k=TOP_K, section=section)
        trace.setdefault("search_rounds", []).append(
            {"round": rnd, "query": query, "section": section or "all", "method": method,
             "top_scores": [h["score"] for h in hits]})
        good = [h for h in hits if h["score"] >= th["min"]]
        # keep the best passages seen across rounds
        seen = {h["chunk_id"] for h in passages}
        passages = sorted(passages + [h for h in hits if h["chunk_id"] not in seen], key=lambda h: -h["score"])[:TOP_K]
        if hits and hits[0]["score"] >= th["top"] and len(good) >= MIN_PASSAGES:
            return passages, True
        query = _rewrite_query(q, p)
    strong = bool(passages) and passages[0]["score"] >= th["top"]
    return passages, strong


def compose_explanation(q: str, passages: list[dict], num: dict | None, usage: llm.Usage) -> list[dict]:
    """LLM writes claims from the retrieved passages only; code drops claims citing unknown ids."""
    allowed = {h["chunk_id"] for h in passages}
    ctx = "\n\n".join(f"[C{h['chunk_id']}] ({h['section']} › {h['heading']})\n{h['text']}" for h in passages)
    figs = ""
    if num and num.get("figures"):
        figs = "\nFigures from the database:\n" + "\n".join(
            f"- {f['period']}: {fmt_value(f['value'], f['unit'])}" for f in num["figures"])
    exp = llm.parse("explain", _EXPLAIN_SYSTEM, f"Question: {q}\n{figs}\n\nPassages:\n{ctx}",
                    Explanation, usage, max_tokens=4000)
    if not exp.answerable:
        return []
    claims = []
    for c in exp.claims:
        ids = [i for i in c.chunk_ids if i in allowed]  # citation existence check
        if ids:
            claims.append({"text": c.text, "chunk_ids": ids})
    return claims


# ============================================================================ main entry
def answer(question: str, method: str = "semantic") -> dict:
    t0 = time.time()
    usage = llm.Usage()
    trace: dict = {"method": method}
    conn = connect()
    res: dict = {"question": question, "status": None, "answer": "", "assumptions": [], "flags": [],
                 "figures": [], "citations": [], "trace": trace}

    def done(status: str, text: str) -> dict:
        res["status"], res["answer"] = status, text
        res["usage"] = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                        "llm_calls": usage.calls, "log": usage.log}
        res["llm"] = llm.model_name()
        res["latency_s"] = round(time.time() - t0, 3)
        conn.close()
        return res

    # 1. parse (LLM + code validation). An LLM failure is reported, not silently replaced.
    try:
        p = parse_question(question, usage)
    except Exception as e:
        usage.log.append({"step": "parse", "error": str(e)[:300]})
        return done("error", f"LLM call failed ({llm.model_name()}): {str(e)[:200]}")
    trace["parse"] = p.model_dump()

    if p.unsupported_reason:
        return done("abstain", f"Out of scope: {p.unsupported_reason}")
    if not p.company:
        names = ", ".join(f"{c['name']} ({c['ticker']})" for c in COMPANIES)
        return done("clarify", f"Which company do you mean? I have 10-Q filings for: {names}.")
    if p.qtype in ("numeric", "both") and not p.metric:
        mets = ", ".join(s["label"].lower() for s in METRICS.values())
        return done("clarify", f"Which figure do you mean? Supported metrics: {mets}.")

    # resolve the period / report
    need_text = p.qtype in ("explanatory", "both")
    if not p.period or p.period == "latest":
        doc = conn.execute(SQL_LATEST_DOC.format(text_filter="AND d.has_text = 1" if need_text else ""),
                           {"ticker": p.company}).fetchone()
        if not doc:
            return done("abstain", "No filings available for this company.")
        period = doc["period"]
        res["assumptions"].append(
            f"No period given; assumed the latest available quarter ({period}, filed {doc['date']}).")
    else:
        period = p.period
        doc = conn.execute(SQL_DOC_FOR_PERIOD, {"ticker": p.company, "period": period}).fetchone()
        if not doc:
            return done("abstain", f"Not enough evidence: no 10-Q for {company_name(p.company)} covering {period} is loaded.")
    trace["period"], trace["report_id"] = period, doc["report_id"]

    # 2. route
    route = {"numeric": ["numeric"], "explanatory": ["explanatory"], "both": ["numeric", "explanatory"]}[p.qtype]
    if p.qtype == "explanatory" and p.metric:
        route = ["numeric", "explanatory"]  # attach the figures being explained (plan 11.5)
        if p.compare == "none":
            p.compare = "yoy"
    trace["route"] = route
    parts: list[str] = []

    # 3a. numeric
    num = None
    if "numeric" in route:
        num = numeric_path(conn, p, period, trace)
        res["flags"] += num["flags"]
        for i, f in enumerate(num["figures"], 1):
            res["figures"].append({"ref": f"F{i}", "value": f["value"], "unit": f["unit"], "period": f["period"],
                                   "metric": f["metric"], "locator": figure_locator(f), "url": f["url"]})
        if num.get("missing"):
            if p.qtype == "numeric":
                return done("abstain", f"Not enough evidence: {num['missing']}")
            res["flags"].append(num["missing"])
        elif num["figures"]:
            parts.append(render_numeric(p, num))
        if "change" in num:
            res["change"] = num["change"]

    # 3b. explanatory
    if "explanatory" in route:
        if not doc["has_text"]:
            return done("abstain", f"Not enough evidence: the text of the {period} filing is not indexed.")
        passages, strong = retrieve(question, p, doc["report_id"], method, trace)
        # 4. check evidence
        if not strong:
            closest = "; ".join(h["locator"] for h in passages[:3])
            msg = f"Not enough evidence in the {period} filing to explain this (best score below threshold). Closest passages: {closest}"
            if parts:
                res["flags"].append(msg)
                return done("partial", " ".join(parts) + "\n\n" + msg)
            return done("abstain", msg)
        try:
            claims = compose_explanation(question, passages, num, usage)
        except Exception as e:
            usage.log.append({"step": "explain", "error": str(e)[:300]})
            return done("error", (" ".join(parts) + "\n\n" if parts else "") + f"LLM call failed: {str(e)[:200]}")
        if not claims:
            msg = "The retrieved passages do not explain this; abstaining on the 'why' part."
            return done("partial" if parts else "abstain", (" ".join(parts) + "\n\n" if parts else "") + msg)
        cited = {}
        for h in passages:
            cited[h["chunk_id"]] = h
        lines = []
        for c in claims:
            refs = "".join(f"[C{i}]" for i in c["chunk_ids"])
            lines.append(f"- {c['text']} {refs}")
        parts.append("Reasons given in the filing:\n" + "\n".join(lines))
        used = {i for c in claims for i in c["chunk_ids"]}
        res["citations"] = [{"ref": f"C{h['chunk_id']}", "locator": h["locator"], "score": h["score"],
                             "url": h["url"], "text": h["text"][:500]} for h in passages if h["chunk_id"] in used]
        res["claims"] = claims

    # 5. output
    return done("answered", "\n\n".join(parts))


def render(res: dict) -> str:
    out = [res["answer"]]
    if res["assumptions"]:
        out.append("Assumptions: " + " ".join(res["assumptions"]))
    if res["flags"]:
        out.append("Flags: " + " | ".join(res["flags"]))
    if res["figures"] or res["citations"]:
        out.append("Sources:")
        for f in res["figures"]:
            out.append(f"  [{f['ref']}] {f['locator']}")
        for c in res["citations"]:
            out.append(f"  [{c['ref']}] {c['locator']} (score {c['score']})")
    u = res.get("usage", {})
    out.append(f"[llm={res.get('llm')} status={res['status']} latency={res.get('latency_s')}s llm_calls={u.get('llm_calls', 0)} "
               f"tokens={u.get('input_tokens', 0)}+{u.get('output_tokens', 0)}]")
    return "\n".join(out)
