"""The Streamlit page renders an API answer (the API is replaced by fake responses)."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "frontend" / "app.py")

HEALTH = {"ok": True, "llm": "claude:claude-opus-5-5", "llm_available": True, "presets": ["claude", "hkust"]}
ANSWER = {
    "question": "What was ACME's revenue in Q2 2023?", "status": "answered",
    "answer": "Acme's net sales for Q2 2023 was $120 [F1].", "assumptions": [], "flags": [],
    "figures": [{"ref": "F1", "value": 120.0, "unit": "USD", "period": "Q2 2023", "metric": "net_sales",
                 "locator": "Acme Form 10-Q for Q2 2023 › Income Statement", "url": "https://www.sec.gov/"}],
    "citations": [], "trace": {}, "usage": {"input_tokens": 1000, "output_tokens": 50, "llm_calls": 1},
    "llm": "claude:claude-haiku-5-5", "latency_s": 1.5,
}


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data

    def raise_for_status(self):
        pass


def test_page_shows_answer_figures_and_metrics(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(HEALTH))
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(ANSWER))

    at = AppTest.from_file(APP).run()
    assert "API connected" in at.sidebar.markdown[0].value
    at.text_input(key="question").set_value(ANSWER["question"]).run()
    next(b for b in at.button if b.label == "Ask").click().run()

    assert not at.exception
    page = "\n".join(m.value for m in at.markdown)
    assert "Answered" in page and "$120" in page
    assert len(at.dataframe) == 1
    assert "1.5 s · 1,000 input / 50 output tokens · 1 LLM calls" in page
