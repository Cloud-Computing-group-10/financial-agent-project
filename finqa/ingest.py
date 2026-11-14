"""Ingestion: download originals, run the tag alignment check, build figures + chunks + index.

Usage:  python -m finqa.ingest            (download if missing, rebuild derived stores)
        python -m finqa.ingest --offline  (rebuild only from files already in data/raw)
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from . import text as textmod
from .config import (ALIGNMENT_REPORT, COMPANIES, FACT_YEARS, METRICS, RAW_DIR, TAG_EXCLUDE,
                     SEC_USER_AGENT, TEXT_YEARS)
from .db import init_db

HEADERS = {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}


# ---------------------------------------------------------------------------- download
def _get(url: str, dest: Path, offline: bool) -> Path:
    if dest.exists() or offline:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    dest.write_bytes(r.content)
    time.sleep(0.15)  # SEC fair-access: stay well under 10 req/s
    return dest


def download(offline: bool = False) -> None:
    for c in COMPANIES:
        cik, t = c["cik"], c["ticker"]
        _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
             RAW_DIR / f"companyfacts_{t}.json", offline)
        sub = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
                   RAW_DIR / f"submissions_{t}.json", offline)
        for f in _ten_qs(json.loads(sub.read_text())):
            if int(f["reportDate"][:4]) in TEXT_YEARS:
                acc = f["accessionNumber"].replace("-", "")
                _get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{f['primaryDocument']}",
                     RAW_DIR / t / f["primaryDocument"], offline)


def _ten_qs(sub: dict) -> list[dict]:
    r = sub["filings"]["recent"]
    keys = ["form", "accessionNumber", "reportDate", "filingDate", "primaryDocument"]
    rows = [dict(zip(keys, vals)) for vals in zip(*(r[k] for k in keys))]
    return [x for x in rows if x["form"] == "10-Q" and int(x["reportDate"][:4]) in FACT_YEARS]


# ---------------------------------------------------------------------------- periods
def quarter_label(end: str) -> tuple[int, int]:
    """Fiscal quarter from period end (shift back 7 days: 52/53-week years end e.g. July 2)."""
    d = date.fromisoformat(end) - timedelta(days=7)
    return (d.month - 1) // 3 + 1, d.year


def period_of(start: str | None, end: str) -> tuple[str, str] | None:
    """Return (label, duration) for a duration fact, or None for unusable durations."""
    if not start:
        return None
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    q, y = quarter_label(end)
    if 80 <= days <= 100:
        return f"Q{q} {y}", "3M"
    for months, lo, hi in [(6, 170, 195), (9, 260, 285)]:
        if lo <= days <= hi:
            return f"{months}M {y}", "YTD"
    return None  # annual or odd durations: out of scope


# ---------------------------------------------------------------------------- alignment
def alignment_check(facts: dict) -> dict:
    """Pick, per company and metric, the first candidate tag with 10-Q quarterly/YTD data."""
    report = {}
    for c in COMPANIES:
        gaap = facts[c["ticker"]]["facts"].get("us-gaap", {})
        report[c["ticker"]] = {}
        for metric, spec in METRICS.items():
            chosen = None
            excluded = {}
            for tag in spec["tags"]:
                if tag not in gaap:
                    continue
                if (c["ticker"], tag) in TAG_EXCLUDE:
                    excluded[tag] = TAG_EXCLUDE[(c["ticker"], tag)]
                    continue
                # must have quarterly/YTD facts for the years in scope (not just old filings)
                n = sum(1 for unit in gaap[tag]["units"].values() for x in unit
                        if x.get("form") == "10-Q" and x.get("fy") in TEXT_YEARS
                        and period_of(x.get("start"), x["end"]))
                if n:
                    chosen = {"tag": tag, "label": gaap[tag].get("label"), "n_facts": n}
                    break
            report[c["ticker"]][metric] = chosen or {"tag": None, "status": "unsupported",
                                                      "tried": spec["tags"], "excluded": excluded}
    return report


# ---------------------------------------------------------------------------- build
def build(offline: bool = False) -> None:
    download(offline)
    conn = init_db(reset=True)
    facts = {c["ticker"]: json.loads((RAW_DIR / f"companyfacts_{c['ticker']}.json").read_text())
             for c in COMPANIES}
    align = alignment_check(facts)
    ALIGNMENT_REPORT.write_text(json.dumps(align, indent=2))

    n_fig = n_chunk = 0
    for c in COMPANIES:
        t = c["ticker"]
        cur = conn.execute("INSERT INTO companies(name, ticker, cik) VALUES (?,?,?)",
                           (c["name"], t, c["cik"]))
        company_id = cur.lastrowid
        sub = json.loads((RAW_DIR / f"submissions_{t}.json").read_text())

        # documents
        report_ids = {}
        for f in _ten_qs(sub):
            q, y = quarter_label(f["reportDate"])
            acc = f["accessionNumber"]
            path = RAW_DIR / t / f["primaryDocument"]
            has_text = int(path.exists() and int(f["reportDate"][:4]) in TEXT_YEARS)
            url = (f"https://www.sec.gov/Archives/edgar/data/{c['cik']}/"
                   f"{acc.replace('-', '')}/{f['primaryDocument']}")
            cur = conn.execute(
                "INSERT INTO documents(company_id,title,form_type,date,period,period_end,accession,url,file_path,has_text)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (company_id, f"{c['name']} Form 10-Q for Q{q} {y}", "10-Q", f["filingDate"],
                 f"Q{q} {y}", f["reportDate"], acc, url,
                 str(path.relative_to(RAW_DIR.parent)) if has_text else None, has_text))
            report_ids[acc] = cur.lastrowid

        # figures (Layer 1: no LLM, no text reading)
        gaap = facts[t]["facts"].get("us-gaap", {})
        for metric, chosen in align[t].items():
            tag = chosen.get("tag")
            if not tag:
                continue
            spec = METRICS[metric]
            seen = set()
            for unit_name, rows in gaap[tag]["units"].items():
                unit = "USD/share" if "/" in unit_name else unit_name
                for x in rows:
                    if x.get("form") != "10-Q" or x["accn"] not in report_ids:
                        continue
                    p = period_of(x.get("start"), x["end"])
                    if not p:
                        continue
                    key = (x["accn"], x["start"], x["end"], unit)
                    if key in seen:
                        continue
                    seen.add(key)
                    conn.execute(
                        "INSERT INTO figures(report_id,statement,metric,tag,line_label,period,period_start,period_end,duration,value,unit)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (report_ids[x["accn"]], spec["statement"], metric, tag, chosen.get("label"),
                         p[0], x["start"], x["end"], p[1], x["val"], unit))
                    n_fig += 1
        n_fig += _derive_quarters_from_ytd(conn, company_id)

        # chunks (Layer 2)
        for f in _ten_qs(sub):
            path = RAW_DIR / t / f["primaryDocument"]
            if not (path.exists() and int(f["reportDate"][:4]) in TEXT_YEARS):
                continue
            rid = report_ids[f["accessionNumber"]]
            seen_text = set()
            for ch in textmod.filing_chunks(path.read_text(errors="ignore")):
                if ch["text"] in seen_text:  # highlights are often repeated verbatim
                    continue
                seen_text.add(ch["text"])
                conn.execute("INSERT INTO chunks(report_id,section,heading,ordinal,text) VALUES (?,?,?,?,?)",
                             (rid, ch["section"], ch["heading"], ch["ordinal"], ch["text"]))
                n_chunk += 1
    conn.commit()
    unsupported = {t: [m for m, v in a.items() if not v.get("tag")] for t, a in align.items()}
    print(f"figures: {n_fig}  chunks: {n_chunk}")
    print(f"alignment check -> {ALIGNMENT_REPORT}; unsupported metrics: {unsupported}")

    from .search import build_index
    build_index(conn)
    conn.close()


def _derive_quarters_from_ytd(conn, company_id: int) -> int:
    """Cash-flow facts are YTD only: Q(n) = YTD(n) - YTD(n-1), within the same filing."""
    rows = conn.execute(
        """SELECT f.* FROM figures f JOIN documents d USING(report_id)
           WHERE d.company_id=? AND f.duration='YTD'""", (company_id,)).fetchall()
    n = 0
    for r in rows:
        q, y = quarter_label(r["period_end"])
        exists = conn.execute(
            "SELECT 1 FROM figures WHERE report_id=? AND metric=? AND period=? AND duration='3M'",
            (r["report_id"], r["metric"], f"Q{q} {y}")).fetchone()
        if exists:
            continue
        prev_months = int(r["period"].split("M")[0]) - 3
        if prev_months == 3:
            prev = conn.execute(
                """SELECT f.value FROM figures f JOIN documents d USING(report_id)
                   WHERE d.company_id=? AND f.metric=? AND f.period_start=? AND f.duration='3M'
                   ORDER BY d.date LIMIT 1""",
                (company_id, r["metric"], r["period_start"])).fetchone()
        else:
            prev = conn.execute(
                """SELECT f.value FROM figures f JOIN documents d USING(report_id)
                   WHERE d.company_id=? AND f.metric=? AND f.period=? AND f.period_start=? AND f.duration='YTD'
                   ORDER BY d.date LIMIT 1""",
                (company_id, r["metric"], f"{prev_months}M {y}", r["period_start"])).fetchone()
        if not prev:
            continue
        conn.execute(
            "INSERT INTO figures(report_id,statement,metric,tag,line_label,period,period_start,period_end,duration,value,unit,is_derived)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
            (r["report_id"], r["statement"], r["metric"], r["tag"], r["line_label"], f"Q{q} {y}",
             None, r["period_end"], "3M", r["value"] - prev["value"], r["unit"]))
        n += 1
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="do not download; use data/raw only")
    build(ap.parse_args().offline)
