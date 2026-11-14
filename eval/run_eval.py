"""Numeric evaluation: workflow vs full-document baseline.

  python eval/verify_answer_key.py                    # (re)build + verify the answer key first
  python eval/run_eval.py --llm claude                 # workflow + baseline with Claude
  python eval/run_eval.py --llm qwen --only system     # workflow only, local Qwen
  python eval/run_eval.py retrieval                    # BM25 vs semantic vs hybrid (explanatory set)

Metrics (per system, per question and aggregated):
  accuracy              the number is right: exact match to the verified answer key for the workflow;
                        for free-text baseline answers within 0.1% (i.e. the filing's own rounding).
                        Change questions also need the prior-year value and the % change (±0.15 pp).
                        Questions the system must decline (expect_status) need an abstention.
  data source accuracy  the cited source is the right one. Workflow: the cited figure comes from the
                        correct 10-Q (accession), statement and period (and the comparison figure from
                        the prior-year period). Baseline: the answer names the correct statement
                        (income statement / cash-flow statement) and the correct period.
  latency               wall-clock seconds per question (median, mean, max).
  input/output tokens   LLM tokens per question (mean) and in total.

Writes eval/results/numeric_<llm>_<timestamp>.json, a per-question CSV and a markdown summary.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finqa import llm  # noqa: E402

HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / "questions_numeric.jsonl"
ANSWER_KEY = HERE / "answer_key_numeric.jsonl"
RESULTS = HERE / "results"


def load(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ============================================================================ scoring helpers
ABSTAIN = re.compile(r"not enough evidence|does not (contain|include|provide|report|disclose|present)|"
                     r"cannot (determine|answer|provide|find)|no (separate |specific )?(line|figure|information|data)|"
                     r"not (available|provided|disclosed|reported|included|presented)|out of scope|i apologize|unable to", re.I)

STATEMENT_PATTERNS = {
    "Income Statement": r"statements? of (consolidated )?(income|earnings|operations)|income statements?|"
                        r"statements? of (comprehensive )?(income|earnings)|results of operations",
    "Cash Flow Statement": r"statements? of cash flows?|cash flows? statements?",
}
MONTHS = {1: "march", 2: "june", 3: "september"}


def numbers_in(text: str) -> list[float]:
    """All numbers in free text, scaled (billion/million) and with parentheses as negatives."""
    vals = []
    for m in re.finditer(r"(\()?\$?\s?(-?\d[\d,]*(?:\.\d+)?)\s*(\))?\s*(billion|million|bn|mn|m\b)?", text, re.I):
        try:
            v = float(m.group(2).replace(",", ""))
        except ValueError:
            continue
        scale = {"billion": 1e9, "bn": 1e9, "million": 1e6, "mn": 1e6, "m": 1e6}.get((m.group(4) or "").lower())
        neg = -1 if (m.group(1) and m.group(3)) or v < 0 else 1
        if scale:
            vals.append(neg * abs(v) * scale)
        else:  # bare numbers: tables are in millions; EPS in dollars
            vals += [neg * abs(v) * 1e6, neg * abs(v)]
    return vals


def has_value(text: str, gold: float, tol: float = 0.001) -> bool:
    return any(abs(v - gold) <= tol * abs(gold) + 1e-9 for v in numbers_in(text)) or \
        any(abs(v - abs(gold)) <= tol * abs(gold) + 1e-9 for v in numbers_in(text))  # "a loss of $6.8bn"


def has_pct(text: str, gold_pct: float) -> bool:
    """% change correct to the precision the answer states: "41%" may round 40.83, "40.8%" may not be 40.6."""
    for p in re.findall(r"(\d+(?:\.(\d+))?)\s*(?:%|percent)", text):
        decimals = len(p[1])
        if abs(float(p[0]) - abs(gold_pct)) <= max(0.15, 0.5 * 10 ** -decimals) + 1e-9:
            return True
    return False


def mentions_period(text: str, period: str) -> bool:
    q, y = int(period[1]), period[3:]
    t = text.lower()
    ordinal = {1: "first", 2: "second", 3: "third"}[q]
    return bool(re.search(rf"\bq{q}\s*'?(20)?{y[2:]}\b|{ordinal} quarter( of)?( fiscal)? {y}|"
                          rf"three months ended {MONTHS[q]} \d{{1,2}}, {y}|three months ended .{{0,40}}{y}", t))


# ============================================================================ scoring
def score_system(q: dict, gold: dict, res: dict) -> dict:
    s = {"accuracy": False, "source_accuracy": False}
    figs = res.get("figures", [])
    if gold.get("expect_status"):
        s["accuracy"] = res.get("status") == gold["expect_status"]
        s["source_accuracy"] = not figs  # declining means no (wrong) figure may be cited
        return s
    if not figs:
        return s
    cur = figs[0]
    ok = abs(cur["value"] - gold["value"]) < 1e-6 * max(1.0, abs(gold["value"]))
    src = gold["accession"] in cur["locator"] and gold["statement"] in cur["locator"] and cur["period"] == gold["period"]
    if "prev_value" in gold:
        ch, prev = res.get("change") or {}, figs[1] if len(figs) > 1 else None
        ok = ok and prev is not None and abs(prev["value"] - gold["prev_value"]) < 1e-6 * max(1.0, abs(gold["prev_value"])) \
            and ch.get("pct_change") is not None and abs(ch["pct_change"] - gold["pct_change"]) < 0.05
        src = src and prev is not None and prev["period"] == gold["prev_period"] and gold["statement"] in prev["locator"]
    s["accuracy"], s["source_accuracy"] = bool(ok), bool(src)
    return s


def score_baseline(q: dict, gold: dict, res: dict) -> dict:
    text = res.get("answer", "")
    if res.get("status") != "answered":  # context overflow / skipped / error: did not answer
        return {"accuracy": False, "source_accuracy": False}
    if gold.get("expect_status"):
        # a correct decline says so up front; listing related, clearly labelled measures is fine
        declined = bool(ABSTAIN.search(text[:400]))
        return {"accuracy": declined, "source_accuracy": declined}
    ok = has_value(text, gold["value"])
    if "prev_value" in gold:
        ok = ok and has_value(text, gold["prev_value"]) and has_pct(text, gold["pct_change"])
    src = bool(re.search(STATEMENT_PATTERNS[gold["statement"]], text, re.I)) and mentions_period(text, gold["period"])
    return {"accuracy": bool(ok), "source_accuracy": bool(src)}


# ============================================================================ runners
def checkpoint_path(system: str) -> Path:
    return RESULTS / "checkpoints" / f"{re.sub(r'[^a-z0-9]+', '-', llm.model_name().lower())}_{system}.jsonl"


def run(system: str, fresh: bool = False) -> tuple[list[dict], bool]:
    """Run one side. Every finished question is appended to a checkpoint file, so a run that stops
    (e.g. API credits run out) resumes where it stopped and already-paid answers are not re-bought.
    Returns (rows, complete)."""
    from finqa.baseline import baseline_answer
    from finqa.workflow import answer
    gold = {r["id"]: r for r in load(ANSWER_KEY)}
    unverified = [k for k, r in gold.items() if not r.get("verified")]
    if unverified:
        print(f"WARNING: answer key has unverified rows {unverified}; run eval/verify_answer_key.py")
    ck = checkpoint_path(system)
    ck.parent.mkdir(parents=True, exist_ok=True)
    if fresh and ck.exists():
        ck.unlink()
    done = {r["id"]: r for r in load(ck)} if ck.exists() else {}
    if done:
        print(f"  resuming: {len(done)} questions already in {ck.name}")
    rows = []
    for q in load(QUESTIONS):
        if q["id"] in done:
            r = done[q["id"]]
            if system == "baseline" and not r["answer"].startswith("[recovered"):
                r.update(score_baseline(q, gold[q["id"]], r))  # re-score saved text with the current grader
            rows.append(r)
            continue
        try:
            if system == "workflow":
                res = answer(q["question"])
                sc = score_system(q, gold[q["id"]], res)
            else:
                res = baseline_answer(q["question"], q["company"], q["period"])
                sc = score_baseline(q, gold[q["id"]], res)
        except Exception as e:  # API error (credits, rate limit, network): stop, keep what is done
            print(f"  ! stopped at {q['id']}: {str(e)[:160]}\n  rerun the same command to resume")
            return rows, False
        u = res.get("usage", {})
        row = {"id": q["id"], "question": q["question"], "status": res.get("status"), **sc,
               "latency_s": res.get("latency_s"), "input_tokens": u.get("input_tokens", 0),
               "output_tokens": u.get("output_tokens", 0), "llm_calls": u.get("llm_calls", 0),
               "answer": res.get("answer", ""), "steps": u.get("log", [])}
        with open(ck, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        rows.append(row)
        mark = "✓" if sc["accuracy"] else "✗"
        print(f"  {mark} {q['id']} {res.get('status'):<16} src={'✓' if sc['source_accuracy'] else '✗'} "
              f"{res.get('latency_s')}s tokens={u.get('input_tokens', 0)}/{u.get('output_tokens', 0)}")
    return rows, True


def summarize(rows: list[dict]) -> dict:
    ran = [r for r in rows if r["status"] not in ("context_overflow", "skipped")]
    lat = [r["latency_s"] for r in ran if r["latency_s"] is not None]
    n = len(rows)
    return {
        "n": n,
        "answered_or_declined": len(ran),
        "accuracy": round(sum(r["accuracy"] for r in rows) / n, 3),
        "source_accuracy": round(sum(r["source_accuracy"] for r in rows) / n, 3),
        "latency_median_s": round(statistics.median(lat), 2) if lat else None,
        "latency_mean_s": round(statistics.mean(lat), 2) if lat else None,
        "latency_max_s": round(max(lat), 2) if lat else None,
        "input_tokens_total": sum(r["input_tokens"] for r in rows),
        "output_tokens_total": sum(r["output_tokens"] for r in rows),
        "input_tokens_per_q": round(sum(r["input_tokens"] for r in ran) / max(len(ran), 1)),
        "output_tokens_per_q": round(sum(r["output_tokens"] for r in ran) / max(len(ran), 1)),
        "context_overflows": sum(1 for r in rows if r["status"] == "context_overflow"),
    }


def md_table(results: dict) -> str:
    lines = ["| | " + " | ".join(results) + " |", "|---|" + "---|" * len(results)]
    def row(label, f):
        lines.append(f"| {label} | " + " | ".join(f(s) for s in results.values()) + " |")
    row("Accuracy", lambda s: f"{s['accuracy']:.0%} ({round(s['accuracy'] * s['n'])}/{s['n']})")
    row("Data source accuracy", lambda s: f"{s['source_accuracy']:.0%} ({round(s['source_accuracy'] * s['n'])}/{s['n']})")
    row("Latency median / mean / max (s)", lambda s: f"{s['latency_median_s']} / {s['latency_mean_s']} / {s['latency_max_s']}")
    row("Input tokens per question", lambda s: f"{s['input_tokens_per_q']:,}")
    row("Output tokens per question", lambda s: f"{s['output_tokens_per_q']:,}")
    row("Input / output tokens total", lambda s: f"{s['input_tokens_total']:,} / {s['output_tokens_total']:,}")
    row("Not run (context overflow)", lambda s: str(s["context_overflows"]))
    return "\n".join(lines)


def run_numeric(only: str | None, fresh: bool = False) -> None:
    name = llm.model_name()
    print(f"LLM: {name} (available={llm.available()})")
    if not llm.available():
        sys.exit("LLM not available (API key / local model / server missing)")
    results, per_q = {}, {}
    for system in (["workflow", "baseline"] if not only else [only.replace("system", "workflow")]):
        print(f"\n== {system} ==")
        rows, complete = run(system, fresh)
        if not complete:
            print(f"  {system}: {len(rows)}/{len(load(QUESTIONS))} done, summary below is partial")
        if not rows:
            continue
        per_q[system] = rows
        label = f"{system} ({name})" + ("" if complete else f" [partial {len(rows)}/{len(load(QUESTIONS))}]")
        results[label] = summarize(rows)
    print("\n" + md_table(results))
    RESULTS.mkdir(exist_ok=True)
    stamp = f"{re.sub(r'[^a-z0-9]+', '-', name.lower())}_{time.strftime('%Y%m%d-%H%M%S')}"
    path = RESULTS / f"numeric_{stamp}.json"
    path.write_text(json.dumps({"llm": name, "summary": results, "per_question": per_q}, indent=2, default=str))
    with open(RESULTS / f"numeric_{stamp}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["system", "id", "status", "accuracy", "source_accuracy", "latency_s", "input_tokens", "output_tokens", "question"])
        for system, rows in per_q.items():
            for r in rows:
                w.writerow([system, r["id"], r["status"], int(r["accuracy"]), int(r["source_accuracy"]),
                            r["latency_s"], r["input_tokens"], r["output_tokens"], r["question"]])
    (RESULTS / f"numeric_{stamp}.md").write_text(f"# Numeric evaluation — {name}\n\n" + md_table(results) + "\n")
    print(f"\n-> {path}")


# ============================================================================ retrieval experiment (kept for later)
def run_retrieval(k_max: int = 5) -> None:
    from finqa.db import connect
    from finqa.search import search
    qs = [q for q in load(HERE / "questions_all_types.jsonl") if q["category"] == "explanatory"]
    conn = connect()

    def hit(text, evidence):
        t = re.sub(r"\s+", " ", text.lower().replace("&", "and"))
        return any(e.lower().replace("&", "and") in t for e in evidence)

    print(f"Retrieval ({len(qs)} explanatory questions, report-restricted, k={k_max})")
    print("| method | hit@1 | hit@3 | hit@5 | MRR |\n|---|---|---|---|---|")
    for method in ["bm25", "semantic", "hybrid"]:
        ranks = []
        for q in qs:
            rid = conn.execute("""SELECT report_id FROM documents d JOIN companies c USING(company_id)
                                  WHERE c.ticker=? AND d.period=?""", (q["company"], q["period"])).fetchone()[0]
            hits = search(q["question"], [rid], method=method, k=k_max)
            ranks.append(next((i + 1 for i, h in enumerate(hits) if hit(h["text"], q["evidence"])), None))
        n = len(ranks)
        print(f"| {method} | {sum(r == 1 for r in ranks) / n:.2f} | {sum(bool(r) and r <= 3 for r in ranks) / n:.2f} | "
              f"{sum(bool(r) for r in ranks) / n:.2f} | {sum(1 / r for r in ranks if r) / n:.2f} |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="numeric", choices=["numeric", "retrieval"])
    ap.add_argument("--llm", help="LLM preset: claude (default), chatgpt, qwen, qwen-14b, mlx, ollama, openai")
    ap.add_argument("--model", help="override the preset's model id")
    ap.add_argument("--only", choices=["system", "baseline"], help="run just one side")
    ap.add_argument("--fresh", action="store_true", help="ignore saved checkpoints and rerun everything")
    a = ap.parse_args()
    llm.configure(a.llm, a.model)
    if a.mode == "retrieval":
        run_retrieval()
    else:
        run_numeric(a.only, a.fresh)


if __name__ == "__main__":
    main()
