"""HTTP load test: many users ask the API at the same time (E4, second part).

  python eval/http_load_test.py --url http://<api-host>:8080 --users 1 10 100 1000 --seconds 60 \
      --name c7i.large_llm0

Each user sends a question of the v2 test set to POST /ask, waits for the answer and asks the next
one right away (no think time), for --seconds per step. The API is eval/stub_api.py, the real API
with a stand-in for the LLM. Results go to eval/results/load/<name>.json: per step the requests,
answers per second, median and p95 latency and the errors (timeouts, refused connections, 5xx).
One asyncio process can keep 1,000 users busy, so the test can run from any machine with a short
network path to the API.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_test import load_questions  # noqa: E402

HERE = Path(__file__).resolve().parent


async def step(url: str, users: int, seconds: int, questions: list[str], timeout: float) -> dict:
    latencies: list[float] = []
    errors: dict[str, int] = {}
    stop = time.monotonic() + seconds
    limits = httpx.Limits(max_connections=users, max_keepalive_connections=users)
    async with httpx.AsyncClient(base_url=url, timeout=timeout, limits=limits) as client:
        async def user(seed: int) -> None:
            rng = random.Random(seed)
            await asyncio.sleep(rng.random())  # spread the first requests over one second
            while time.monotonic() < stop:
                t = time.monotonic()
                try:
                    r = await client.post("/ask", json={"question": rng.choice(questions)})
                    if r.status_code != 200:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    latencies.append(time.monotonic() - t)
                except Exception as e:  # count it and keep going, as a real user would retry
                    kind = type(e).__name__ if not isinstance(e, RuntimeError) else str(e)
                    errors[kind] = errors.get(kind, 0) + 1
        start = time.monotonic()
        await asyncio.gather(*(user(i) for i in range(users)))
        elapsed = time.monotonic() - start
    lat = sorted(latencies)
    pick = (lambda f: round(lat[min(int(len(lat) * f), len(lat) - 1)] * 1000)) if lat else (lambda f: None)
    return {"users": users, "requests": len(lat), "per_second": round(len(lat) / elapsed, 1),
            "median_ms": pick(0.5), "p95_ms": pick(0.95), "p99_ms": pick(0.99),
            "errors": errors, "error_rate": round(sum(errors.values()) / max(1, len(lat) + sum(errors.values())), 4)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--users", type=int, nargs="+", default=[1, 10, 50, 100, 250, 500, 1000])
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--timeout", type=float, default=60, help="a request slower than this counts as an error")
    ap.add_argument("--name", required=True, help="file name, e.g. <instance type>_llm<seconds>")
    args = ap.parse_args()

    questions = [q["question"] for q in load_questions()]
    httpx.post(f"{args.url}/ask", json={"question": questions[0]}, timeout=120).raise_for_status()  # warm up
    meta = httpx.get(f"{args.url}/health", timeout=10).json()
    rows = []
    for users in args.users:
        row = asyncio.run(step(args.url, users, args.seconds, questions, args.timeout))
        rows.append(row)
        print(row, flush=True)
    out = HERE / "results" / "load" / f"{args.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"name": args.name, "url": args.url, "health": meta, "steps": rows}, indent=1))
    print("wrote", out)


if __name__ == "__main__":
    main()
