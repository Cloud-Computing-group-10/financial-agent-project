"""Turn one company's SEC XBRL facts (a "companyfacts" JSON file) into rows for our tables.

Plain Python with no database or third-party imports. The same code runs in the pilot ingest
(finqa/ingest.py) and, on every SEC filer, inside the Spark job (spark/facts_job.py).

Rules (design plan 15.1):
  * Only 10-Q filings are used.
  * A fact spanning about 3 months is a quarter ("Q2 2023"); 6- and 9-month facts are
    year-to-date ("6M 2023"). The label comes from the fact's own dates, not from the
    filing's fy/fp fields (a Q2 2023 filing also reports Q2 2022 as a comparison).
  * Quarters are calendar quarters of the period end date.
  * Companies use different XBRL tags for the same thing. For each filing and metric, the first
    candidate tag in config.METRICS that the filing reports is used.
  * Cash-flow statements in a 10-Q are year-to-date only, so a quarter is derived as
    YTD(n) - YTD(n-1) and marked is_derived.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .config import METRICS, TAG_EXCLUDE

FORM = "10-Q"


# ---------------------------------------------------------------------------- periods
def quarter_label(end: str) -> str:
    """Calendar quarter of a period end date, e.g. '2023-07-01' -> 'Q2 2023'.

    The date is moved back 7 days first: 52/53-week fiscal years end a few days after the
    calendar quarter (e.g. on July 1)."""
    d = date.fromisoformat(end) - timedelta(days=7)
    return f"Q{(d.month - 1) // 3 + 1} {d.year}"


def period_of(start: str | None, end: str) -> tuple[str, str] | None:
    """(label, duration) for a duration fact, e.g. ('Q2 2023', '3M') or ('6M 2023', 'YTD').

    Returns None for point-in-time facts and for durations we do not use (e.g. full years)."""
    if not start:
        return None
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    quarter = quarter_label(end)
    year = quarter.split()[1]
    if 80 <= days <= 100:
        return quarter, "3M"
    if 170 <= days <= 195:
        return f"6M {year}", "YTD"
    if 260 <= days <= 285:
        return f"9M {year}", "YTD"
    return None


# ---------------------------------------------------------------------------- names
_NAME_SUFFIXES = {"the", "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "plc", "llc"}


def display_name(entity_name: str) -> str:
    """SEC names are often all caps ('COCA COLA CO'); show them in title case instead."""
    return entity_name.title() if entity_name.isupper() else entity_name


def name_key(name: str) -> str:
    """Normalised company name for lookups: 'The Coca-Cola Company' -> 'coca cola'."""
    words = re.sub(r"[^a-z0-9]+", " ", name.lower().replace("&", " and ")).split()
    return " ".join(w for w in words if w not in _NAME_SUFFIXES)


# ---------------------------------------------------------------------------- extraction
def extract_company(data: dict, ticker: str | None = None, years: tuple[int, int] | None = None,
                    name: str | None = None) -> dict[str, list[dict]]:
    """All rows for one company. `years` (first, last) keeps 10-Qs whose quarter ends in that range.
    `name` overrides the SEC entity name (used for the pilot companies).

    Returns {"companies": [...], "documents": [...], "figures": [...], "metric_tags": [...]}.
    Figures refer to their filing by accession number; the loader turns that into report_id."""
    cik = int(data["cik"])
    name = name or display_name(data["entityName"])
    gaap = data.get("facts", {}).get("us-gaap", {})

    documents = _documents(cik, name, gaap, years)
    figures, metric_tags = [], []
    for metric, spec in METRICS.items():
        rows, tags_used, excluded = _metric_figures(cik, spec, gaap, documents)
        rows += _derive_quarters(rows, documents)
        for row in rows:
            row["metric"] = metric
        figures += rows
        metric_tags.append({
            "company_id": cik,
            "metric": metric,
            "tags": ", ".join(tags_used) or None,
            "unsupported_reason": None if tags_used else _unsupported_reason(spec, excluded),
        })

    company = {"company_id": cik, "name": name, "ticker": ticker, "name_key": name_key(name)}
    return {"companies": [company], "documents": list(documents.values()),
            "figures": figures, "metric_tags": metric_tags}


def _documents(cik: int, name: str, gaap: dict, years: tuple[int, int] | None) -> dict[str, dict]:
    """One row per 10-Q filing, keyed by accession number.

    The quarter a filing covers is the latest end date among its 3-month facts."""
    filings: dict[str, dict] = {}
    for concept in gaap.values():
        for facts in concept["units"].values():
            for f in facts:
                if f.get("form") != FORM:
                    continue
                period = period_of(f.get("start"), f["end"])
                if not period or period[1] != "3M":
                    continue
                doc = filings.setdefault(f["accn"], {"filed": f["filed"], "period_end": f["end"]})
                doc["period_end"] = max(doc["period_end"], f["end"])

    documents = {}
    for accession, doc in filings.items():
        period = quarter_label(doc["period_end"])
        year = int(period.split()[1])
        if years and not years[0] <= year <= years[1]:
            continue
        documents[accession] = {
            "accession": accession,
            "company_id": cik,
            "form_type": FORM,
            "title": f"{name} Form 10-Q for {period}",
            "filed": doc["filed"],
            "period": period,
            "period_end": doc["period_end"],
            # Filing index page; the pilot ingest replaces it with the link to the 10-Q document.
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/",
        }
    return documents


def _metric_figures(cik: int, spec: dict, gaap: dict, documents: dict) -> tuple[list[dict], list[str], list[str]]:
    """Figures for one metric. Returns (rows, tags used, reasons for excluded tags)."""
    rows: list[dict] = []
    tags_used: list[str] = []
    excluded: list[str] = []
    claimed: set[str] = set()  # filings that already have a figure from an earlier candidate tag

    for tag in spec["tags"]:
        if tag not in gaap:
            continue
        if (cik, tag) in TAG_EXCLUDE:
            excluded.append(TAG_EXCLUDE[(cik, tag)])
            continue
        tag_rows: list[dict] = []
        seen: set[tuple] = set()
        for unit_name, facts in gaap[tag]["units"].items():
            unit = "USD/share" if "/" in unit_name else unit_name
            for f in facts:
                if f.get("form") != FORM or f["accn"] not in documents or f["accn"] in claimed:
                    continue
                period = period_of(f.get("start"), f["end"])
                key = (f["accn"], f.get("start"), f["end"], unit)
                if not period or key in seen:
                    continue
                seen.add(key)
                tag_rows.append({
                    "accession": f["accn"],
                    "statement": spec["statement"],
                    "tag": tag,
                    "line_label": gaap[tag].get("label"),
                    "period": period[0],
                    "period_start": f["start"],
                    "period_end": f["end"],
                    "duration": period[1],
                    "value": float(f["val"]),
                    "unit": unit,
                    "is_derived": False,
                })
        if tag_rows:
            tags_used.append(tag)
            claimed |= {r["accession"] for r in tag_rows}
            rows += tag_rows
    return rows, tags_used, excluded


def _derive_quarters(rows: list[dict], documents: dict) -> list[dict]:
    """Quarter = YTD(n) - YTD(n-1): 6M minus Q1, or 9M minus 6M, with the same start date.

    Only for filings that do not report the quarter directly (typical for cash flows).
    The earlier piece is taken from the first filing that reported it."""
    earliest: dict[tuple, float] = {}
    for r in sorted(rows, key=lambda r: documents[r["accession"]]["filed"]):
        months = 3 if r["duration"] == "3M" else int(r["period"].split("M")[0])
        earliest.setdefault((r["period_start"], months, r["unit"]), r["value"])

    have = {(r["accession"], r["period"]) for r in rows if r["duration"] == "3M"}
    derived = []
    for r in rows:
        if r["duration"] != "YTD":
            continue
        quarter = quarter_label(r["period_end"])
        if (r["accession"], quarter) in have:
            continue
        months = int(r["period"].split("M")[0])
        previous = earliest.get((r["period_start"], months - 3, r["unit"]))
        if previous is None:
            continue
        derived.append({**r, "period": quarter, "period_start": None, "duration": "3M",
                        "value": r["value"] - previous, "is_derived": True})
        have.add((r["accession"], quarter))
    return derived


def _unsupported_reason(spec: dict, excluded: list[str]) -> str:
    if excluded:
        return "; ".join(excluded)
    return "none of the XBRL tags " + ", ".join(spec["tags"]) + " is reported in its 10-Qs"
