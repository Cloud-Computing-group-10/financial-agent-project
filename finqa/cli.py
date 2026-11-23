"""CLI:  python -m finqa.cli "Why did 3M's sales drop in Q2 2023?" [--method bm25] [--json]
                          [--llm claude|hkust|qwen|...] [--model MODEL_ID]
       python -m finqa.cli "What was 3M's revenue in Q2 2023?" --baseline --ticker MMM --period 'Q2 2023'"""
from __future__ import annotations

import argparse
import json

from dotenv import load_dotenv

from .workflow import answer, render


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--method", default="hybrid", choices=["semantic", "bm25", "hybrid"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-writer", action="store_true", help="template answer instead of the LLM-written one")
    ap.add_argument("--baseline", action="store_true", help="run the full-document LLM baseline instead")
    ap.add_argument("--ticker", help="baseline only: company of the filing to give the model")
    ap.add_argument("--period", help="baseline only: quarter of the filing, e.g. 'Q2 2023'")
    ap.add_argument("--llm", help="LLM preset: claude (default), hkust, chatgpt, qwen, qwen-14b, mlx, ollama, openai")
    ap.add_argument("--model", help="override the preset's model id (e.g. claude-sonnet-5-5, qwen2.5:14b-instruct)")
    a = ap.parse_args()
    if a.baseline and not (a.ticker and a.period):
        ap.error("--baseline needs --ticker and --period")
    from . import llm
    llm.configure(a.llm, a.model)
    if a.baseline:
        from .baseline import baseline_answer
        res = baseline_answer(a.question, a.ticker, a.period)
        print(json.dumps(res, indent=2) if a.json else res["answer"])
        return
    res = answer(a.question, method=a.method, writer=False if a.no_writer else None)
    print(json.dumps(res, indent=2, default=str) if a.json else render(res))


if __name__ == "__main__":
    main()
