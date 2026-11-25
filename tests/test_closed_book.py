"""Closed-book baseline (system N) and its grading, without any LLM or database."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

from finqa import closed_book  # noqa: E402
from run_experiments import grade_row  # noqa: E402

Q = {"question": "What was 3M's revenue in Q2 2023?", "category": "lookup", "gold": {"value": 8325e6}}


def test_closed_book_sends_only_the_question(monkeypatch):
    sent = {}
    monkeypatch.setattr(closed_book.llm, "text", lambda name, system, user, usage, **kw: sent.update(user=user) or "$8.3 billion")
    res = closed_book.closed_book_answer(Q["question"])
    assert sent["user"] == f"Question: {Q['question']}"
    assert res["status"] == "answered" and res["answer"] == "$8.3 billion"


def test_closed_book_numeric_grades():
    def grade(text):
        return grade_row(Q, {"system": "N", "status": "answered", "answer": text}, judge=None)["verdict"]
    assert grade("3M's revenue was $8.3 billion in Q2 2023.") == "correct"
    assert grade("About $9.1 billion.") == "incorrect"
    assert grade("I don't have reliable data on 3M's Q2 2023 revenue.") == "declined"
