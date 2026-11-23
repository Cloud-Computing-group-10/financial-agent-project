"""Load test of the pipeline without the LLM (plan E4): how many questions per second can the
database and search layer of one instance serve, and how does latency grow with concurrent users?

  python eval/load_test.py --users 1 10 50 --seconds 60

Each simulated user repeatedly takes a question of the v2 test set and runs the full workflow
(company lookup, filing lookup, SQL figures, MD&A search with question embedding) with the LLM
steps replaced: the parse comes from the question's known fields, the "why" claims are the top
passages, and the writer is off. So the numbers measure our own pipeline, not an LLM provider.
Run it on the instance (next to Postgres), e.g. via docker compose run.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finqa import workflow  # noqa: E402
from finqa.parse import ParsedQuestion  # noqa: E402

HERE = Path(__file__).resolve().parent
NAMES = {"MMM": "3M", "KO": "Coca-Cola", "JNJ": "Johnson & Johnson", "CAT": "Caterpillar", "HON": "Honeywell",
         "PEP": "PepsiCo", "TSLA": "Tesla", "ABT": "Abbott", "IBM": "IBM", "LMT": "Lockheed Martin"}


def known_parse(q: dict) -> ParsedQuestion:
    return ParsedQuestion(company=NAMES[q["company"]], metric=q.get("metric"), period=q.get("period"),
                          qtype="explanatory" if q["category"] == "why" else "numeric",
                          compare=q.get("compare") or "none")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--users", type=int, nargs="+", default=[1, 10, 50])
    ap.add_argument("--seconds", type=int, default=60)
    args = ap.parse_args()

    questions = [q for q in map(json.loads, (HERE / "questions_v2.jsonl").open())
                 if q["split"] == "test" and q.get("company") in NAMES and q.get("expect") == "answer"
                 and q.get("period") != "latest"]
    parses = {q["question"]: known_parse(q) for q in questions}
    workflow.parse_question = lambda question, usage: parses[question]
    workflow.compose_explanation = lambda question, passages, num, usage: [
        {"text": p["text"][:200], "chunk_ids": [p["chunk_id"]]} for p in passages[:2]]
    workflow.answer(questions[0]["question"], writer=False)  # load the embedding model once

    results = []
    for users in args.users:
        latencies: list[float] = []
        lock = threading.Lock()
        stop = time.time() + args.seconds

        def user(seed: int) -> None:
            rng = random.Random(seed)
            while time.time() < stop:
                q = rng.choice(questions)
                t = time.perf_counter()
                res = workflow.answer(q["question"], writer=False)
                if res["status"] not in ("answered", "partial", "abstain", "clarify"):
                    raise RuntimeError(res["answer"])
                with lock:
                    latencies.append(time.perf_counter() - t)

        threads = [threading.Thread(target=user, args=(i,)) for i in range(users)]
        start = time.time()
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        elapsed = time.time() - start
        lat = sorted(latencies)
        row = {"users": users, "requests": len(lat), "per_second": round(len(lat) / elapsed, 1),
               "median_ms": round(lat[len(lat) // 2] * 1000), "p95_ms": round(lat[int(len(lat) * 0.95)] * 1000)}
        results.append(row)
        print(row, flush=True)
    out = HERE / "results" / "load_test.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
