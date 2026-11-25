"""Rules that turn XBRL facts into figures (finqa/facts.py)."""
from conftest import A1, A2, FACTS

from finqa.facts import display_name, extract_company, name_key, period_of, quarter_label


def test_quarter_label_uses_calendar_quarter():
    assert quarter_label("2023-06-30") == "Q2 2023"
    assert quarter_label("2023-07-01") == "Q2 2023"  # 52/53-week year ending just after the quarter
    assert quarter_label("2023-12-31") == "Q4 2023"


def test_period_of_quarter_ytd_and_others():
    assert period_of("2023-04-01", "2023-06-30") == ("Q2 2023", "3M")
    assert period_of("2023-01-01", "2023-06-30") == ("6M 2023", "YTD")
    assert period_of("2023-01-01", "2023-09-30") == ("9M 2023", "YTD")
    assert period_of("2023-01-01", "2023-12-31") is None  # full year: out of scope
    assert period_of(None, "2023-06-30") is None          # point-in-time fact


def test_names():
    assert name_key("The Coca-Cola Company") == "coca cola"
    assert name_key("Johnson & Johnson") == "johnson and johnson"
    assert display_name("ACME WIDGETS INC") == "Acme Widgets Inc"
    assert display_name("Johnson & Johnson") == "Johnson & Johnson"


def by_key(figures):
    return {(f["accession"], f["metric"], f["period"]): f for f in figures}


def test_extract_documents_ignore_10k():
    rows = extract_company(FACTS, ticker="ACME")
    assert {d["accession"]: d["period"] for d in rows["documents"]} == {A1: "Q1 2023", A2: "Q2 2023"}
    assert rows["companies"][0] == {"company_id": 1, "name": "Acme Widgets Inc", "ticker": "ACME",
                                    "name_key": "acme widgets"}


def test_each_filing_uses_its_own_first_candidate_tag():
    figures = by_key(extract_company(FACTS)["figures"])
    assert figures[(A2, "net_sales", "Q2 2023")]["tag"] == "Revenues"
    assert figures[(A1, "net_sales", "Q1 2023")]["tag"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert figures[(A2, "net_sales", "Q2 2022")]["value"] == 100.0  # comparative from a later filing


def test_cash_flow_quarter_is_derived_from_ytd():
    figures = by_key(extract_company(FACTS)["figures"])
    q2 = figures[(A2, "operating_cash_flow", "Q2 2023")]
    assert q2["value"] == 75.0 - 30.0
    assert q2["is_derived"] and q2["duration"] == "3M"


def test_units_and_unsupported_metrics():
    rows = extract_company(FACTS)
    eps = by_key(rows["figures"])[(A2, "eps_diluted", "Q2 2023")]
    assert eps["unit"] == "USD/share"
    tags = {m["metric"]: m for m in rows["metric_tags"]}
    assert tags["net_sales"]["tags"] == "Revenues, RevenueFromContractWithCustomerExcludingAssessedTax"
    assert tags["net_income"]["tags"] is None  # only a 10-K fact
    assert "none of the XBRL tags" in tags["net_income"]["unsupported_reason"]


def test_years_filter():
    rows = extract_company(FACTS, years=(2024, 2025))
    assert rows["documents"] == [] and rows["figures"] == []
