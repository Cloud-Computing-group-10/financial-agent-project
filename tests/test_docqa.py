"""Document QA path and the router: pure functions, no database or LLM."""
from datetime import date

import pytest

from finqa.docqa import evaluate, printed_in
from finqa.parse import ParsedQuestion, find_period
from finqa.pdf_docs import document_row, pdf_passages
from finqa.workflow import structured


def test_evaluate_allows_only_arithmetic():
    assert evaluate("(a + b) / 2 / c * 365", {"a": 10, "b": 30, "c": 100}) == pytest.approx(73.0)
    assert evaluate("-x ** 2", {"x": 3}) == -9
    for bad in ("__import__('os')", "a.real", "open('f')", "[a]"):
        with pytest.raises(ValueError):
            evaluate(bad, {"a": 1})


def test_printed_in_matches_whole_numbers_only():
    text = "Property, plant and equipment — net\n8,738\n8,866\nCash $ 4,258 (1,577) margin 12.5%"
    assert all(printed_in(v, text) for v in (8738, 8866, 4258, 1577, 12.5))
    assert not any(printed_in(v, text) for v in (873, 738, 12, 9999))


def test_pdf_passages_stay_on_their_page():
    pages = ["Consolidated Balance Sheet\n" + "\n".join(f"Line item {i} 1,{i:03d}" for i in range(60)), "", "Short"]
    passages = pdf_passages(pages)
    assert {p["section"] for p in passages} == {"page 1"}  # empty and tiny pages give nothing
    assert all(p["heading"] == "Consolidated Balance Sheet" for p in passages)
    assert [p["ordinal"] for p in passages] == list(range(len(passages)))


def test_financebench_document_periods():
    doc = {"company": "3M", "doc_type": "10q", "doc_link": "x", "doc_period": 2023, "doc_name": "3M_2023Q2_10Q"}
    assert document_row(doc)["period"] == "Q2 2023"
    assert document_row({**doc, "doc_type": "10k", "doc_name": "3M_2018_10K", "doc_period": 2018})["period"] == "FY 2018"
    row = document_row({**doc, "doc_type": "8k", "doc_name": "PEPSICO_2023_8K_dated-2023-05-05", "company": "PepsiCo"})
    assert (row["period"], row["period_end"]) == ("8-K 2023-05-05", date(2023, 5, 5))


def test_find_period_full_years():
    assert find_period("What is the FY2018 capex for 3M?") == "FY 2018"
    assert find_period("net income in fiscal year 2022") == "FY 2022"
    assert find_period("FY22 revenue") == "FY 2022"
    assert find_period("revenue in Q2 2023") == "Q2 2023"


def test_router_sends_only_quarterly_metrics_and_whys_to_the_structured_path():
    q = dict(company="3M", qtype="numeric", compare="none")
    assert structured(ParsedQuestion(**q, metric="net_sales", period="Q2 2023"))
    assert structured(ParsedQuestion(**{**q, "qtype": "explanatory"}, period="Q2 2023"))
    assert not structured(ParsedQuestion(**q, metric="net_sales", period="FY 2018"))   # annual: 10-K
    assert not structured(ParsedQuestion(**q, period="Q2 2023"))                       # capex, ratios ...
    assert not structured(ParsedQuestion(**q, metric="net_sales", period="Q2 2023", unsupported_reason="ratio"))
