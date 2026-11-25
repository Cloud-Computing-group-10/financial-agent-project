"""Answer key for the v2 question set (eval/questions_v2.jsonl), checked against the filings themselves.

  python eval/build_answer_key.py            (needs the full database, e.g. on the EC2 instance)

Per category:
  lookup / change / paraphrase / latest   the value comes from the figures table (with the same
      rules as the workflow); it is `verified` only if a table row of the 10-Q whose label matches
      the metric contains it. A year-over-year change compares with the prior-year column of the
      same 10-Q, so that row must hold both values; a quarter-over-quarter change checks each value
      in its own filing. Cash-flow quarters are YTD differences, so the two YTD values are checked.
  why       every evidence phrase must occur in the filing's text; the key lists which ones do.
  decline   the reason is checked where the database can show it: an unknown company has no
            match, an unreported metric has no tag (metric_tags).
Filings are read from data/ when their text is downloaded, otherwise fetched from SEC once and
cached in data/eval_filings/.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finqa.companies import company_by_ticker, find_companies  # noqa: E402
from finqa.config import DATA_DIR, SEC_USER_AGENT  # noqa: E402
from finqa.db import connect  # noqa: E402
from finqa.text import html_to_lines, read_filing  # noqa: E402
from finqa.workflow import SQL_LATEST_DOC, lookup, previous_figure, shift_period  # noqa: E402

# A table row matches when its label fits the metric and it holds the value as printed.
LABELS = {
    "net_sales": r"sales|revenue",
    "cost_of_sales": r"cost of (goods|products|sales|revenue)",
    "gross_profit": r"gross profit",
    "sga": r"selling|general and administrative",
    "rd_expense": r"research",
    "operating_income": r"operating (income|profit|earnings)|income from operations",
    "pretax_income": r"before (provision for )?(income )?tax",
    "net_income": r"net (income|earnings|loss)|profit( \(loss\))? attributable|profit$",
    "eps_diluted": r"diluted|assuming dilution",
    "operating_cash_flow": r"operating activities",
}


def fmt_cell(v: float, unit: str) -> str:
    """How the value is printed in the filing's tables (millions, or dollars for EPS)."""
    if unit == "USD/share":
        s = f"{abs(v):.2f}"
    else:
        s = f"{abs(v) / 1e6:,.0f}"
    return f"({s})" if v < 0 else s


def rows_with(lines: list[str], metric: str, *cells: str) -> list[str]:
    out = []
    for line in lines:
        if " | " not in line:
            continue
        # "( 6,841 )" -> "(6,841)"; drop footnote markers like "Profit 1" / "diluted 2"
        parts = [re.sub(r"\(\s*([\d.,]+)\s*\)", r"(\1)", p.strip().lstrip("$").strip()) for p in line.split(" | ")]
        label = re.sub(r"\s+\d$", "", parts[0])
        if not re.search(LABELS[metric], label, re.I):
            continue
        if all(c in parts[1:] for c in cells):
            out.append(line)
    return out


HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / "questions_v2.jsonl"
ANSWER_KEY = HERE / "answer_key_v2.jsonl"
CACHE = DATA_DIR / "eval_filings"
NUMERIC = {"lookup", "change", "paraphrase", "latest"}

_lines: dict[str, list[str]] = {}


def filing_lines(doc: dict) -> list[str]:
    """The 10-Q as text lines (tables become 'label | cell | cell' rows)."""
    if doc["accession"] not in _lines:
        path = DATA_DIR / doc["file_path"] if doc["file_path"] else CACHE / f"{doc['accession']}.htm"
        if not path.exists():
            CACHE.mkdir(parents=True, exist_ok=True)
            r = requests.get(doc["url"], headers={"User-Agent": SEC_USER_AGENT}, timeout=60)
            r.raise_for_status()
            path.write_bytes(r.content)
        _lines[doc["accession"]] = html_to_lines(read_filing(path))
    return _lines[doc["accession"]]


def document(conn, company_id: int, period: str) -> dict | None:
    if period == "latest":
        return conn.execute(SQL_LATEST_DOC, {"company_id": company_id, "need_text": False}).fetchone()
    return conn.execute("SELECT * FROM documents WHERE company_id = %s AND period = %s",
                        (company_id, period)).fetchone()


# The year-to-date figure just before a derived quarter: same start of year, ending earlier
# (for Q2 that is the plain Q1 figure, for Q3 the 6-month YTD), from the filing that reported it first.
SQL_EARLIER_YTD = """
SELECT f.value, f.unit, d.accession
FROM figures f JOIN documents d USING (report_id)
WHERE d.company_id = %(company_id)s AND f.metric = %(metric)s AND NOT f.is_derived
  AND f.period_start = %(start)s AND f.period_end < %(end)s
ORDER BY f.period_end DESC, d.filed LIMIT 1
"""


def verify_figure(conn, company_id: int, metric: str, fig: dict) -> list[str]:
    """Table rows that show the figure: the value itself, or for a derived quarter the two
    year-to-date values it was computed from (each in the filing that reports it)."""
    doc = conn.execute("SELECT * FROM documents WHERE report_id = %s", (fig["report_id"],)).fetchone()
    if not fig["is_derived"]:
        return rows_with(filing_lines(doc), metric, fmt_cell(fig["value"], fig["unit"]))
    ytd = conn.execute("SELECT f.value, f.unit, f.period_start FROM figures f WHERE f.report_id = %s "
                       "AND f.metric = %s AND f.duration <> '3M' AND f.period_end = %s ORDER BY f.period_start LIMIT 1",
                       (fig["report_id"], metric, fig["period_end"])).fetchone()
    if not ytd:
        return []
    earlier = conn.execute(SQL_EARLIER_YTD, {"company_id": company_id, "metric": metric,
                                             "start": ytd["period_start"], "end": fig["period_end"]}).fetchone()
    if not earlier:
        return []
    hits = rows_with(filing_lines(doc), metric, fmt_cell(ytd["value"], ytd["unit"]))
    earlier_doc = conn.execute("SELECT * FROM documents WHERE accession = %s AND company_id = %s",
                               (earlier["accession"], company_id)).fetchone()
    earlier_hits = rows_with(filing_lines(earlier_doc), metric, fmt_cell(earlier["value"], earlier["unit"]))
    return [f"YTD {hits[0]} minus earlier YTD {earlier_hits[0]}"] if hits and earlier_hits else []


def numeric_key(conn, q: dict) -> dict:
    company = company_by_ticker(conn, q["company"])
    doc = document(conn, company["company_id"], q["period"])
    period = doc["period"]
    cur, _ = lookup(conn, company["company_id"], q["metric"], period)
    row = {"id": q["id"], "period": period, "accession": doc["accession"], "value": cur and cur["value"],
           "unit": cur and cur["unit"], "statement": cur and cur["statement"],
           "line_label": cur and cur["line_label"], "derived": bool(cur and cur["is_derived"])}
    if not cur:
        return {**row, "verified": False, "note": "no figure in the database"}

    if q.get("compare") not in ("yoy", "qoq"):
        hits = verify_figure(conn, company["company_id"], q["metric"], cur)
        return {**row, "verified": bool(hits), "evidence_row": hits[0][:200] if hits else None}

    row["prev_period"] = shift_period(period, q["compare"])
    prev, _ = previous_figure(conn, company["company_id"], q["metric"], row["prev_period"], cur, q["compare"])
    if not prev:
        return {**row, "verified": False, "note": "no figure for the comparison period"}
    row["prev_value"] = prev["value"]
    row["prev_accession"] = prev["accession"]
    row["abs_change"] = cur["value"] - prev["value"]
    row["pct_change"] = round(row["abs_change"] * 100 / abs(prev["value"]), 2) if prev["value"] else None
    if prev["report_id"] == cur["report_id"] and not cur["is_derived"]:
        # current and prior-year column of one line item
        hits = rows_with(filing_lines(doc), q["metric"], fmt_cell(cur["value"], cur["unit"]),
                         fmt_cell(prev["value"], prev["unit"]))
    else:
        hits = verify_figure(conn, company["company_id"], q["metric"], cur)
        prev_hits = verify_figure(conn, company["company_id"], q["metric"], prev)
        row["prev_evidence_row"] = prev_hits[0][:200] if prev_hits else None
        hits = hits if prev_hits else []
    return {**row, "verified": bool(hits), "evidence_row": hits[0][:200] if hits else None}


def why_key(conn, q: dict) -> dict:
    company = company_by_ticker(conn, q["company"])
    doc = document(conn, company["company_id"], q["period"])
    text = " ".join(" ".join(filing_lines(doc)).lower().split())
    found = [p for p in q["evidence"] if p in text]
    return {"id": q["id"], "period": doc["period"], "accession": doc["accession"], "reason": q["reason"],
            "evidence": q["evidence"], "evidence_found": found, "verified": bool(found)}


def decline_key(conn, q: dict) -> dict:
    row = {"id": q["id"], "expect": q["expect"], "decline_reason": q["decline_reason"]}
    if q["decline_reason"].startswith("unknown company"):
        mention = re.match(r"What was (.+?)'s", q["question"]).group(1)
        row["verified"] = find_companies(conn, mention) == []
    elif q["decline_reason"].startswith("metric not reported"):
        company = company_by_ticker(conn, q["company"])
        tags = conn.execute("SELECT tags FROM metric_tags WHERE company_id = %s AND metric = %s",
                            (company["company_id"], q["metric"])).fetchone()
        row["verified"] = tags is not None and tags["tags"] is None
    else:
        row["verified"] = True  # decided by the scope rules (Q4, 10-K, ratios) or by missing evidence
    return row


def main() -> None:
    conn = connect()
    questions = [json.loads(line) for line in QUESTIONS.read_text().splitlines() if line.strip()]
    out = []
    for q in questions:
        if q["category"] in NUMERIC:
            row = numeric_key(conn, q)
            detail = row.get("evidence_row") or row.get("note")
        elif q["category"] == "why":
            row = why_key(conn, q)
            detail = f"found {len(row['evidence_found'])}/{len(q['evidence'])}: {row['evidence_found']}"
        else:
            row = decline_key(conn, q)
            detail = q["decline_reason"]
        row["split"] = q["split"]
        out.append(row)
        print(f"{'OK ' if row['verified'] else '?? '}{q['id']} {q.get('company', ''):<5} {q['question'][:60]:<60} {str(detail)[:90]}")

    ANSWER_KEY.write_text("\n".join(json.dumps(r) for r in out) + "\n")
    ok = sum(r["verified"] for r in out)
    print(f"\n{ok}/{len(out)} verified -> {ANSWER_KEY}")


if __name__ == "__main__":
    main()
