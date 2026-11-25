"""CLI:  python -m finqa.cli "Why did 3M's sales drop in Q2 2023?" [--method bm25] [--json]
                          [--llm claude|hkust|qwen|...] [--model MODEL_ID] [--no-writer]"""
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
    ap.add_argument("--llm", help="LLM preset: claude (default), hkust, chatgpt, qwen, qwen-3b, qwen-14b, ollama, ollama-3b, openai")
    ap.add_argument("--model", help="override the preset's model id (e.g. claude-sonnet-5-5, qwen2.5:14b-instruct)")
    a = ap.parse_args()
    from . import llm
    llm.configure(a.llm, a.model)
    res = answer(a.question, method=a.method, writer=False if a.no_writer else None)
    print(json.dumps(res, indent=2, default=str) if a.json else render(res))


if __name__ == "__main__":
    main()
