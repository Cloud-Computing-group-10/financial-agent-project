"""Loading, company lookup and the numeric path against a real Postgres (the LLM is replaced)."""
import json

import pytest
from conftest import FACTS, TEST_DATABASE_URL

from finqa import db, workflow
from finqa.companies import find_companies
from finqa.facts import extract_company
from finqa.parse import ParsedQuestion


def other_company(cik, name, ticker=None):
    """A copy of FACTS for another filer, with its own accession numbers."""
    data = json.loads(json.dumps(FACTS).replace("0000000001-", f"{cik:010d}-"))
    return extract_company({**data, "cik": cik, "entityName": name}, ticker=ticker)


@pytest.fixture
def loaded(conn):
    for rows in (extract_company(FACTS, ticker="ACME"),
                 other_company(2, "ACME HOLDINGS LTD"),           # unlisted namesake
                 other_company(3, "Globex Corp", ticker="GBX")):
        db.load_facts(conn, rows)
    return conn


def test_load_is_idempotent(loaded):
    before = loaded.execute("SELECT count(*) AS n FROM figures").fetchone()["n"]
    assert db.load_facts(loaded, extract_company(FACTS, ticker="ACME")) == 0  # already there: skipped
    assert loaded.execute("SELECT count(*) AS n FROM figures").fetchone()["n"] == before


def test_find_companies(loaded):
    assert [c["company_id"] for c in find_companies(loaded, "GBX")] == [3]          # ticker
    assert [c["company_id"] for c in find_companies(loaded, "gbx")] == [3]          # any case
    assert [c["company_id"] for c in find_companies(loaded, "Globex")] == [3]       # name
    assert [c["company_id"] for c in find_companies(loaded, "Acme")] == [1]         # listed filer wins
    assert find_companies(loaded, "Initech") == []


def ask(monkeypatch, parsed: ParsedQuestion, writer_reply: str | None = None) -> dict:
    """Run the workflow with a fixed parse instead of an LLM call. With writer_reply, the answer
    writer is on and its LLM call returns that text; otherwise the template answer is used."""
    monkeypatch.setattr(workflow, "parse_question", lambda q, usage: parsed)
    monkeypatch.setattr(workflow, "connect", lambda: db.connect(TEST_DATABASE_URL))
    monkeypatch.setattr(workflow.llm, "text", lambda *args, **kwargs: writer_reply)
    return workflow.answer("test question", writer=writer_reply is not None)


def test_numeric_lookup_with_year_over_year_change(loaded, monkeypatch):
    res = ask(monkeypatch, ParsedQuestion(company="ACME", metric="net_sales", period="Q2 2023",
                                          qtype="numeric", compare="yoy"))
    assert res["status"] == "answered"
    assert [f["value"] for f in res["figures"]] == [120.0, 100.0]
    assert res["change"]["pct_change"] == 20.0
    assert "0000000001-23-000002" in res["figures"][0]["locator"]  # cited filing comes from the database


def test_unsupported_metric_is_declined_with_reason(loaded, monkeypatch):
    res = ask(monkeypatch, ParsedQuestion(company="ACME", metric="net_income", period="Q2 2023", qtype="numeric"))
    assert res["status"] == "abstain"
    assert "none of the XBRL tags" in res["answer"]


def test_unknown_company_and_missing_period(loaded, monkeypatch):
    assert ask(monkeypatch, ParsedQuestion(company="Initech", metric="net_sales", qtype="numeric"))["status"] == "abstain"
    res = ask(monkeypatch, ParsedQuestion(company="ACME", metric="net_sales", qtype="numeric"))
    assert res["status"] == "answered"
    assert "assumed the latest available quarter (Q2 2023" in res["assumptions"][0]


def test_jointly_filed_10q_keeps_each_companys_figures(conn):
    """Some 10-Qs are filed by a parent and a subsidiary together: same accession, two companies."""
    parent = extract_company(FACTS, ticker="ACME")
    subsidiary = json.loads(json.dumps(FACTS).replace("120.0", "12.0"))  # same filings, other numbers
    db.load_facts(conn, parent)
    db.load_facts(conn, extract_company({**subsidiary, "cik": 5, "entityName": "ACME POWER CO"}))
    rows = conn.execute("""SELECT d.company_id, f.value FROM figures f JOIN documents d USING (report_id)
                           WHERE f.metric = 'net_sales' AND f.period = 'Q2 2023' ORDER BY 1""").fetchall()
    assert [(r["company_id"], r["value"]) for r in rows] == [(1, 120.0), (5, 12.0)]


YOY = ParsedQuestion(company="ACME", metric="net_sales", period="Q2 2023", qtype="numeric", compare="yoy")


def test_writer_answer_is_used_when_it_keeps_the_facts(loaded, monkeypatch):
    reply = "Acme's revenue was $120 [F1], up 20.0% from $100 a year earlier [F2]."
    res = ask(monkeypatch, YOY, writer_reply=reply)
    assert res["answer"] == reply and res["trace"]["writer"]["used"]
    assert res["answer_template"].startswith("Acme Widgets Inc's net sales")


def test_writer_answer_with_a_changed_number_is_rejected(loaded, monkeypatch):
    res = ask(monkeypatch, YOY, writer_reply="Acme's revenue was $125 [F1], up 25% [F2].")
    assert res["answer"] == res["answer_template"]          # falls back to the template
    assert "numbers not in the facts" in res["trace"]["writer"]["problems"][0]
    res = ask(monkeypatch, YOY, writer_reply="Acme's revenue was $120 [F7].")
    assert "unknown references" in res["trace"]["writer"]["problems"][0]


def test_a_dominant_listed_namesake_wins(conn):
    db.load_facts(conn, extract_company(FACTS, ticker="AWI"))
    db.load_facts(conn, other_company(7, "ACME AEROSPACE INC", ticker="AAI"))
    assert len(find_companies(conn, "Acme")) == 2           # two listed namesakes, 2 filings each: ask
    for i in range(6):                                        # ACME gets 8 filings, 4x as many
        conn.execute("""INSERT INTO documents (company_id, accession, form_type, title, filed, period, period_end)
                        VALUES (1, %s, '10-Q', 't', '2020-01-01', 'Q1 2020', '2020-03-31')""", (f"x-{i}",))
    assert [c["company_id"] for c in find_companies(conn, "Acme")] == [1]
