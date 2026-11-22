"""Filings that come as PDF files: the documents of the FinanceBench test set.

  python -m finqa.pdf_docs            load data/financebench/pdfs into the database

FinanceBench (Patronus AI) asks 150 questions about 84 filings of 32 US companies: mostly 10-Ks,
some 10-Qs, earnings releases and 8-Ks. They are stored like our 10-Qs (a documents row and
embedded passages in chunks), so the document QA path and the baselines can search them.
Differences to the 10-Q text:
  * one passage per piece of a PDF page (about 150 words), labelled with the page number;
  * tables are kept, because many FinanceBench questions need numbers from the statements;
  * the accession column holds "financebench:<doc_name>", and the period is the one in the name
    (FY 2019, Q2 2023: the company's own fiscal labels).
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from .config import DATA_DIR
from .db import connect, init_db
from .embed import embed, passage_text

FINANCEBENCH = DATA_DIR / "financebench"
PIECE_WORDS = 150  # MiniLM reads about 200 words; shorter pieces keep a table row and its labels together

# FinanceBench company name -> SEC CIK (company_id)
COMPANIES = {
    "3M": 66740, "AES Corporation": 874761, "AMD": 2488, "Activision Blizzard": 718877, "Adobe": 796343,
    "Amazon": 1018724, "Amcor": 1748790, "American Express": 4962, "American Water Works": 1410636,
    "Best Buy": 764478, "Block": 1512673, "Boeing": 12927, "CVS Health": 64803, "Coca-Cola": 21344,
    "Corning": 24741, "Costco": 909832, "Foot Locker": 850209, "General Mills": 40704, "JPMorgan": 19617,
    "Johnson & Johnson": 200406, "Kraft Heinz": 1637459, "Lockheed Martin": 936468, "MGM Resorts": 789570,
    "Microsoft": 789019, "Netflix": 1065280, "Nike": 320187, "Paypal": 1633917, "PepsiCo": 77476,
    "Pfizer": 78003, "Ulta Beauty": 1403568, "Verizon": 732712, "Walmart": 104169,
}
FORMS = {"10k": "10-K", "10q": "10-Q", "8k": "8-K", "Earnings": "Earnings release"}
QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def page_texts(pdf: Path) -> list[str]:
    """Text of every page. Parsing is slow, so the result is cached next to the PDF."""
    cache = pdf.with_suffix(".pages.json")
    if cache.exists():
        return json.loads(cache.read_text())
    from pypdf import PdfReader
    pages = [page.extract_text() or "" for page in PdfReader(pdf).pages]
    cache.write_text(json.dumps(pages))
    return pages


def pdf_passages(pages: list[str]) -> list[dict]:
    """Pieces of about PIECE_WORDS words, cut at line ends, never across pages."""
    passages = []
    for number, page in enumerate(pages, 1):
        lines = [re.sub(r"\s+", " ", line.replace("\x00", "")).strip() for line in page.split("\n")]
        lines = [line for line in lines if line and not re.fullmatch(r"(?i)table of contents|\d{1,3}", line)]
        heading = next((line for line in lines if len(line) > 3 and not line[0].isdigit()), "")[:120]
        buf: list[str] = []
        for line in lines + [None]:
            if line is not None:
                buf.append(line)
            if buf and (line is None or sum(len(b.split()) for b in buf) >= PIECE_WORDS):
                text = "\n".join(buf)
                if len(text.split()) >= 15:
                    passages.append({"section": f"page {number}", "heading": heading, "text": text})
                buf = []
    for i, p in enumerate(passages):
        p["ordinal"] = i
    return passages


def document_row(doc: dict) -> dict:
    """documents row for one FinanceBench document (period from its name, e.g. 3M_2023Q2_10Q)."""
    name, year = doc["doc_name"], int(doc["doc_period"])
    quarter = re.search(r"_(\d{4})Q([1-4])_", name)
    dated = re.search(r"dated[-_](\d{4})-(\d{2})-(\d{2})", name)
    if quarter:
        year, q = int(quarter.group(1)), int(quarter.group(2))
        period, end = f"Q{q} {year}", date(year, *QUARTER_END[q])
    elif dated:
        end = date(*map(int, dated.groups()))
        period = f"8-K {end.isoformat()}"
    else:
        period, end = f"FY {year}", date(year, 12, 31)
    return {"company_id": COMPANIES[doc["company"]], "accession": f"financebench:{name}",
            "form_type": FORMS[doc["doc_type"]], "title": f"{doc['company']} {FORMS[doc['doc_type']]} {period}",
            "filed": end, "period": period, "period_end": end, "url": doc["doc_link"],
            "file_path": f"financebench/pdfs/{name}.pdf"}


SQL_INSERT_DOCUMENT = """
INSERT INTO documents (company_id, accession, form_type, title, filed, period, period_end, url, file_path, has_text)
VALUES (%(company_id)s, %(accession)s, %(form_type)s, %(title)s, %(filed)s, %(period)s, %(period_end)s,
        %(url)s, %(file_path)s, TRUE)
ON CONFLICT (company_id, accession) DO NOTHING
RETURNING report_id
"""


def load(conn, folder: Path = FINANCEBENCH) -> None:
    init_db(conn)
    docs = [json.loads(line) for line in (folder / "financebench_document_information.jsonl").open()]
    used = {json.loads(line)["doc_name"] for line in (folder / "financebench_open_source.jsonl").open()}
    for doc in sorted((d for d in docs if d["doc_name"] in used), key=lambda d: d["doc_name"]):
        row = document_row(doc)
        if not conn.execute("SELECT 1 FROM companies WHERE company_id = %s", (row["company_id"],)).fetchone():
            print(f"skip {doc['doc_name']}: company {row['company_id']} is not in the database")
            continue
        inserted = conn.execute(SQL_INSERT_DOCUMENT, row).fetchone()
        if not inserted:
            continue  # loaded before
        passages = pdf_passages(page_texts(folder / "pdfs" / f"{doc['doc_name']}.pdf"))
        vectors = embed([passage_text(p["heading"], p["text"]) for p in passages])
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO chunks (report_id, section, heading, ordinal, text, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                [(inserted["report_id"], p["section"], p["heading"], p["ordinal"], p["text"], v)
                 for p, v in zip(passages, vectors)])
        conn.commit()
        print(f"{doc['doc_name']}: {len(passages)} passages", flush=True)


if __name__ == "__main__":
    load_dotenv()
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    with connect() as conn:
        load(conn)
