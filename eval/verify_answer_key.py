"""Build the numeric answer key and verify every value against the filing document itself.

  python eval/verify_answer_key.py

1. Draft: gold values come from the SEC structured data (figures table).
2. Verify: each value is searched for in the 10-Q's own HTML (the human-readable statements),
   which is an independent rendering of the numbers. A value is `verified` only if a table row
   whose label matches the metric contains it; for year-over-year questions the same row must also
   contain the prior-year value (current and comparative columns of one line item).
The matching rows are printed so a person can eyeball them, and they are stored in the key.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finqa.config import DATA_DIR  # noqa: E402
from finqa.db import connect  # noqa: E402
from finqa.text import html_to_lines  # noqa: E402
from finqa.workflow import lookup, shift_period  # noqa: E402

HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / "questions_numeric.jsonl"
ANSWER_KEY = HERE / "answer_key_numeric.jsonl"

LABELS = {
    "net_sales": r"sales|revenue",
    "cost_of_sales": r"cost of (goods|products|sales|revenue)",
    "gross_profit": r"gross profit",
    "sga": r"selling|general and administrative",
    "rd_expense": r"research",
    "operating_income": r"operating (income|profit)|income from operations",
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


def main() -> None:
    conn = connect()
    qs = [json.loads(l) for l in QUESTIONS.read_text().splitlines() if l.strip()]
    cache: dict[str, list[str]] = {}
    out, ok = [], 0
    for q in qs:
        cur, _ = lookup(conn, q["company"], q["metric"], q["period"])
        doc = conn.execute("""SELECT d.* FROM documents d JOIN companies c USING(company_id)
                              WHERE c.ticker=? AND d.period=?""", (q["company"], q["period"])).fetchone()
        if q.get("expect_status"):  # questions the system must decline (e.g. metric not reported)
            row = {"id": q["id"], "expect_status": q["expect_status"], "value": None,
                   "verified": cur is None, "note": q.get("note")}
            ok += row["verified"]
            print(f"{'OK ' if cur is None else '?? '}{q['id']} {q['company']} {q['metric']:<19} {q['period']} "
                  f"expect {q['expect_status']} (no figure stored: {cur is None})")
            out.append(row)
            continue
        row = {"id": q["id"], "value": cur and cur["value"], "unit": cur and cur["unit"],
               "accession": doc["accession"], "statement": cur and cur["statement"],
               "line_label": cur and cur["line_label"], "period": q["period"], "derived": bool(cur and cur["is_derived"])}
        cells = [fmt_cell(cur["value"], cur["unit"])] if cur else []
        if q["compare"] == "yoy":
            prev, _ = lookup(conn, q["company"], q["metric"], shift_period(q["period"], "yoy"))
            row["prev_value"] = prev and prev["value"]
            row["prev_period"] = shift_period(q["period"], "yoy")
            if cur and prev and prev["value"]:
                row["pct_change"] = round((cur["value"] - prev["value"]) * 100 / abs(prev["value"]), 2)
                cells.append(fmt_cell(prev["value"], prev["unit"]))
        if doc["file_path"] not in cache:
            cache[doc["file_path"]] = html_to_lines((DATA_DIR / doc["file_path"]).read_text(errors="ignore"))
        hits = rows_with(cache[doc["file_path"]], q["metric"], *cells) if cur else []
        row["verified"] = bool(hits)
        row["evidence_row"] = hits[0][:200] if hits else None
        ok += row["verified"]
        mark = "OK " if hits else "?? "
        print(f"{mark}{q['id']} {q['company']} {q['metric']:<19} {q['period']} cells={cells}")
        print(f"      {hits[0][:150] if hits else 'NO MATCHING ROW - check by hand'}")
        out.append(row)
    ANSWER_KEY.write_text("\n".join(json.dumps(r) for r in out) + "\n")
    print(f"\n{ok}/{len(out)} verified against the filing documents -> {ANSWER_KEY}")


if __name__ == "__main__":
    main()
