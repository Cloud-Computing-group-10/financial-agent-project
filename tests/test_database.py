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


def ask(monkeypatch, parsed: ParsedQuestion) -> dict:
    """Run the workflow with a fixed parse instead of an LLM call."""
    monkeypatch.setattr(workflow, "parse_question", lambda q, usage: parsed)
    monkeypatch.setattr(workflow, "connect", lambda: db.connect(TEST_DATABASE_URL))
    return workflow.answer("test question")


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
