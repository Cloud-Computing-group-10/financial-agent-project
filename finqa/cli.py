"""CLI:  python -m finqa.cli "Why did 3M's sales drop in Q2 2023?" [--method bm25] [--json] [--baseline]
                          [--llm claude|qwen|qwen-14b|ollama|...] [--model MODEL_ID]"""
from __future__ import annotations

import argparse
import json

from .workflow import answer, render


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--method", default="semantic", choices=["semantic", "bm25", "hybrid"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--baseline", action="store_true", help="run the full-document LLM baseline instead")
    ap.add_argument("--llm", help="LLM preset: claude (default), chatgpt, qwen, qwen-14b, mlx, ollama, openai")
    ap.add_argument("--model", help="override the preset's model id (e.g. claude-sonnet-5-5, qwen2.5:14b-instruct)")
    a = ap.parse_args()
    from . import llm
    llm.configure(a.llm, a.model)
    if a.baseline:
        from .baseline import baseline_answer
        res = baseline_answer(a.question)
        print(json.dumps(res, indent=2) if a.json else res["answer"])
        return
    res = answer(a.question, method=a.method)
    print(json.dumps(res, indent=2, default=str) if a.json else render(res))


if __name__ == "__main__":
    main()
