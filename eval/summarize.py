"""All experiment results as one Markdown report: eval/RESULTS_v2.md.

  python eval/summarize.py

Reads only saved files (no database, no LLM): the graded answers of eval/run_experiments.py, the
retrieval experiment, the scaling and load-test results. Every table in the report comes from here.
"""
from __future__ import annotations

import csv
import json
import statistics as st
from pathlib import Path

HERE = Path(__file__).resolve().parent
R = HERE / "results"
PRICES = {"claude-haiku-5-5": (0.10, 0.50)}  # USD per million input / output tokens (23 Nov 2026)
NUMERIC = {"lookup", "change", "paraphrase", "latest"}
MODEL_NAMES = {"claude:claude-haiku-5-5": "Haiku 5.5 (API)",
               "mlx:mlx-community/Qwen2.5-7B-Instruct-4bit": "Qwen2.5-7B (Mac, mlx)",
               "mlx:mlx-community/Qwen2.5-3B-Instruct-4bit": "Qwen2.5-3B (Mac, mlx)",
               "openai:qwen2.5:3b-instruct": "Qwen2.5-3B (EC2 CPU, Ollama)"}
SYSTEM_NAMES = {"A": "A workflow", "A0": "A0 workflow, template answer", "C": "C simple RAG", "N": "N closed book (no retrieval)"}


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def questions(set_name: str) -> dict[str, dict]:
    if set_name == "v2":
        return {q["id"]: q for q in load(HERE / "questions_v2.jsonl")}
    return {q["financebench_id"]: {**q, "category": q["question_type"], "split": "test"}
            for q in load(HERE.parent / "data" / "financebench" / "financebench_open_source.jsonl")}


def median(values) -> float:
    values = [v for v in values if v is not None]
    return st.median(values) if values else float("nan")


def cost(rows: list[dict], model: str) -> str:
    price = next((p for name, p in PRICES.items() if name in model), None)
    if not price:
        return "$0 (local)"
    usd = sum(r["input_tokens"] * price[0] + r["output_tokens"] * price[1] for r in rows) / 1e6
    return f"${usd * 1000 / len(rows):.2f}"


def accuracy_table(set_name: str) -> list[str]:
    qs = questions(set_name)
    files = sorted((R / set_name).glob("graded_*.jsonl"))
    out = ["| System | Model | n | Accuracy | Declined, should | Declined, should not | Median s | Tokens in / out | Cost / 1,000 q |",
           "|---|---|---|---|---|---|---|---|---|"]
    by_cat = []
    cats = sorted({qs[r["id"]]["category"] for f in files for r in load(f) if r["id"] in qs})
    for f in files:
        rows = [r for r in load(f) if qs.get(r["id"], {}).get("split") == "test"]
        if not rows:
            continue
        should = [r for r in rows if qs[r["id"]].get("expect") in ("decline", "clarify")]
        other = [r for r in rows if r not in should]
        model = rows[0]["model"]
        name = MODEL_NAMES.get(model, model)
        out.append(f"| {SYSTEM_NAMES[rows[0]['system']]} | {name} | {len(rows)} | "
                   f"**{sum(r['correct'] for r in rows)}/{len(rows)}** ({100 * sum(r['correct'] for r in rows) / len(rows):.0f}%) | "
                   f"{sum(r['verdict'] == 'declined' for r in should)}/{len(should)} | "
                   f"{sum(r['verdict'] == 'declined' for r in other)}/{len(other)} | "
                   f"{median(r['latency_s'] for r in rows):.1f} | "
                   f"{round(sum(r['input_tokens'] for r in rows) / len(rows)):,} / {round(sum(r['output_tokens'] for r in rows) / len(rows)):,} | "
                   f"{cost(rows, model)} |")
        cells = []
        for c in cats:
            v = [r["correct"] for r in rows if qs[r["id"]]["category"] == c]
            cells.append(f"{sum(v)}/{len(v)}" if v else "–")
        by_cat.append(f"| {SYSTEM_NAMES[rows[0]['system']]} | {name} | " + " | ".join(cells) + " |")
    out += ["", "Accuracy per category:", "", "| System | Model | " + " | ".join(cats) + " |",
            "|---|---|" + "---|" * len(cats)] + by_cat
    return out


def writer_table() -> list[str]:
    out = ["| Model | Answers written | Writer answer used | Rejected by the check (template used) |", "|---|---|---|---|"]
    for f in sorted((R / "v2").glob("A__*.jsonl")):
        rows = [r for r in load(f) if r.get("writer")]
        if rows:
            used = sum(r["writer"]["used"] for r in rows)
            out.append(f"| {MODEL_NAMES.get(rows[0]['model'], rows[0]['model'])} | {len(rows)} | {used} | {len(rows) - used} |")
    return out


def retrieval_tables() -> list[str]:
    out = []
    for set_name in ("v2", "financebench"):
        data = json.loads((R / "retrieval" / f"{set_name}.json").read_text()) if (R / "retrieval" / f"{set_name}.json").exists() else {}
        if not data:
            continue
        out += [f"**{'Our \"why\" questions (test split)' if set_name == 'v2' else 'FinanceBench (150 questions)'}**", "",
                "| Method | Scope | n | hit@1 | hit@3 | hit@5 | hit@10 | MRR@10 | median ms |", "|---|---|---|---|---|---|---|---|---|"]
        for method, rows in data.items():
            for scope in sorted({r["scope"] for r in rows}):
                sel = [r for r in rows if r["scope"] == scope and (set_name != "v2" or r["split"] == "test")]
                hits = [sum(r["first_hit"] is not None and r["first_hit"] <= k for r in sel) / len(sel) for k in (1, 3, 5, 10)]
                mrr = sum(1 / r["first_hit"] for r in sel if r["first_hit"]) / len(sel)
                out.append(f"| {method} | {scope} | {len(sel)} | " + " | ".join(f"{h:.2f}" for h in hits)
                           + f" | {mrr:.2f} | {median(r['seconds'] for r in sel) * 1000:.0f} |")
        out.append("")
    return out


def latency_table() -> list[str]:
    rows = load(R / "v2" / "A__openai_qwen2.5_3b-instruct.jsonl")
    qs = questions("v2")
    steps = ["llm_parse", "sql_company", "sql_filing", "sql_figures", "search_embed", "search_db",
             "search_rank", "llm_explain", "llm_writer"]
    out = ["| Step | Numeric questions (median ms) | \"Why\" questions (median ms) |", "|---|---|---|"]
    groups = [[r for r in rows if qs[r["id"]]["category"] in NUMERIC and r["status"] == "answered" and r.get("timings")],
              [r for r in rows if qs[r["id"]]["category"] == "why" and r["status"] in ("answered", "partial") and r.get("timings")]]
    for s in steps:
        cells = []
        for g in groups:
            v = [r["timings"][s] for r in g if s in r["timings"]]
            cells.append(f"{median(v) * 1000:,.1f}" if v else "–")
        out.append(f"| {s} | " + " | ".join(cells) + " |")
    out.append("| **total** | " + " | ".join(f"{median(r['latency_s'] for r in g) * 1000:,.0f}" for g in groups) + " |")
    return out


def main() -> None:
    lines = ["# Experiment results (v2, 23 November 2026)", "",
             "Generated by `eval/summarize.py` from the files in `eval/results/`. Test split only.", "",
             "## E1 Accuracy: our question set (50 test questions)", ""] + accuracy_table("v2")
    lines += ["", "## E1 Accuracy: FinanceBench", "",
              "Haiku: all 150 questions. Qwen: a fixed random sample of 50 (seed 0), the same for every system.", ""]
    lines += accuracy_table("financebench")
    lines += ["", "## Answer writer (A vs A0)", ""] + writer_table()
    lines += ["", "## E2 Retrieval (no LLM)", ""] + retrieval_tables()
    lines += ["## E3 Latency per step (EC2 c7i.8xlarge, Qwen2.5-3B on CPU via Ollama, no network between steps)", ""] + latency_table()
    load_test = json.loads((R / "load_test_c7i.8xlarge.json").read_text()) if (R / "load_test_c7i.8xlarge.json").exists() else []
    lines += ["", "## E4 Load test of the pipeline without the LLM (c7i.8xlarge, one process)", "",
              "| Concurrent users | Questions / s | Median ms | p95 ms |", "|---|---|---|---|"]
    lines += [f"| {r['users']} | {r['per_second']} | {r['median_ms']} | {r['p95_ms']} |" for r in load_test]
    scaling = list(csv.DictReader((R / "scaling.csv").open())) if (R / "scaling.csv").exists() else []
    lines += ["", "## E5 Spark scaling on one machine (c7i.8xlarge, 32 vCPU = 16 cores x 2 threads)", "",
              "| Cores | Text job: s (80 filings, 2,458 passages) | Passages / s | Speed-up | Facts job: s (20,609 files) | Speed-up |",
              "|---|---|---|---|---|---|"]
    text = {int(r["cores"]): r for r in scaling if r["job"] == "text"}
    facts = {int(r["cores"]): r for r in scaling if r["job"] == "facts"}
    for c in sorted(text):
        lines.append(f"| {c} | {text[c]['seconds']} | {text[c]['rate']} | {float(text[1]['seconds']) / float(text[c]['seconds']):.1f}x | "
                     f"{facts[c]['seconds']} | {float(facts[1]['seconds']) / float(facts[c]['seconds']):.1f}x |")
    (HERE / "RESULTS_v2.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
