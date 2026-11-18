"""Deterministic checks in the parser and the search ranking (no LLM, no database)."""
from finqa.parse import find_period, scope_violation
from finqa.search import _rrf_scores, tokenize
from finqa.workflow import fmt_value, shift_period


def test_find_period():
    assert find_period("What was revenue in Q2 2023?") == "Q2 2023"
    assert find_period("sales in the second quarter of 2023") == "Q2 2023"
    assert find_period("2023 q3 results") == "Q3 2023"
    assert find_period("why did sales drop last quarter") == "latest"
    assert find_period("what was revenue") is None


def test_scope_rules():
    assert "10-K" in scope_violation("What was 3M's annual revenue in 2023?")
    assert "Q4" in scope_violation("What was 3M's revenue in Q4 2023?")
    assert "ratio" in scope_violation("What was 3M's gross margin in Q2 2023?")
    assert scope_violation("What was 3M's revenue in Q2 2023?") is None


def test_shift_period():
    assert shift_period("Q2 2023", "yoy") == "Q2 2022"
    assert shift_period("Q1 2023", "qoq") == "Q4 2022"


def test_fmt_value():
    assert fmt_value(8_325_000_000, "USD") == "$8.325 billion"
    assert fmt_value(-377_000_000, "USD") == "-$377 million"
    assert fmt_value(1.25, "USD/share") == "$1.25 per share"


def test_tokenize_drops_stop_words():
    assert tokenize("Why did the sales drop?") == ["sales", "drop"]


def test_rrf_rewards_agreement():
    semantic = [0.9, 0.1, 0.5]
    bm25 = [10.0, 0.0, 12.0]
    fused = _rrf_scores(semantic, bm25)
    assert fused[1] == min(fused)  # last in both rankings
