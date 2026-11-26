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
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-opus-5-5": (4.00, 20.00)}  # USD per million input / output tokens
NUMERIC = {"lookup", "change", "paraphrase", "latest"}
MODEL_NAMES = {"claude:claude-haiku-5-5": "Haiku 5.5 (API)", "claude:claude-opus-5-5": "Opus 5.5 (API)",
               "mlx:mlx-community/Qwen2.5-7B-Instruct-4bit": "Qwen2.5-7B (Mac, mlx)",
               "mlx:mlx-community/Qwen2.5-3B-Instruct-4bit": "Qwen2.5-3B (Mac, mlx)",
               "openai:qwen2.5:3b-instruct": "Qwen2.5-3B (EC2 CPU, Ollama)"}
SYSTEM_NAMES = {"A": "A workflow", "A0": "A0 workflow, template answer", "A2": "A2 workflow v2 (+ statements)",
                "C": "C simple RAG", "N": "N closed book (no retrieval)"}


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


def runs_by_combination(set_name: str) -> dict[tuple[str, str], list[list[dict]]]:
    """{(system, model): [rows of run 1, rows of run 2, ...]} from the graded files, test split only.
    Run 1 is graded_<system>__<model>.jsonl, later runs end in __r2, __r3."""
    qs = questions(set_name)
    out: dict[tuple[str, str], list[list[dict]]] = {}
    for f in sorted((R / set_name).glob("graded_*.jsonl")):
        rows = [r for r in load(f) if qs.get(r["id"], {}).get("split") == "test"]
        if rows:
            out.setdefault((rows[0]["system"], rows[0]["model"]), []).append(rows)
    return out


def mean_std(values: list[float], fmt: str = "{:.1f}") -> str:
    """'m ± s' over runs; just 'm' for a single run."""
    if len(values) == 1:
        return f"{values[0]:g}" if float(values[0]).is_integer() else fmt.format(values[0])
    return f"{fmt.format(st.mean(values))} ± {fmt.format(st.stdev(values))}"


def bootstrap_ci(per_question: list[float], draws: int = 2000, seed: int = 0) -> tuple[float, float]:
    """95% interval of the mean accuracy when the questions are resampled (percentile bootstrap)."""
    import random
    rng = random.Random(seed)
    n = len(per_question)
    means = sorted(sum(rng.choices(per_question, k=n)) / n for _ in range(draws))
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def accuracy_table(set_name: str) -> list[str]:
    """One row per system and model. Accuracy: mean ± std over the runs (k runs) and the 95%
    bootstrap interval over questions (each question scored by its mean over the runs)."""
    qs = questions(set_name)
    combos = runs_by_combination(set_name)
    out = ["| System | Model | n | runs | Accuracy % (mean ± std) | 95% CI | Correct | Declined | Wrong | "
           "Declined, should | Latency s (mean ± std) | Tokens in / out | Cost / 1,000 q |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    cats = sorted({qs[r["id"]]["category"] for runs in combos.values() for r in runs[0]})
    by_cat = []
    for (system, model), runs in sorted(combos.items(), key=lambda kv: (list(SYSTEM_NAMES).index(kv[0][0]), kv[0][1])):
        n = len(runs[0])
        name = MODEL_NAMES.get(model, model)
        acc = [100 * sum(r["correct"] for r in rows) / len(rows) for rows in runs]
        per_q: dict[str, list[float]] = {}
        for rows in runs:
            for r in rows:
                per_q.setdefault(r["id"], []).append(float(r["correct"]))
        lo, hi = bootstrap_ci([st.mean(v) for v in per_q.values()])
        should = lambda r: qs[r["id"]].get("expect") in ("decline", "clarify")  # noqa: E731
        correct = [sum(r["correct"] for r in rows) for rows in runs]
        declined = [sum(r["verdict"] == "declined" and not should(r) for r in rows) for rows in runs]
        wrong = [len(rows) - c - d - sum(should(r) and not r["correct"] and r["verdict"] == "declined" for r in rows)
                 for rows, c, d in zip(runs, correct, declined)]
        should_n = sum(should(r) for r in runs[0])
        should_ok = [sum(should(r) and r["verdict"] == "declined" for r in rows) for rows in runs]
        latency = [r["latency_s"] for rows in runs for r in rows if r.get("latency_s") is not None]
        all_rows = [r for rows in runs for r in rows]
        out.append(
            f"| {SYSTEM_NAMES[system]} | {name} | {n} | {len(runs)} | **{mean_std(acc)}** | {100 * lo:.0f}–{100 * hi:.0f} | "
            f"{mean_std(correct)} | {mean_std(declined)} | {mean_std(wrong)} | "
            f"{mean_std(should_ok)}/{should_n} | "
            f"{st.mean(latency):.1f} ± {st.stdev(latency):.1f} | "
            f"{round(sum(r['input_tokens'] for r in all_rows) / len(all_rows)):,} / "
            f"{round(sum(r['output_tokens'] for r in all_rows) / len(all_rows)):,} | {cost(all_rows, model)} |")
        cells = []
        for c in cats:
            v = [[r["correct"] for r in rows if qs[r["id"]]["category"] == c] for rows in runs]
            cells.append(f"{mean_std([sum(x) for x in v])}/{len(v[0])}" if v[0] else "–")
        by_cat.append(f"| {SYSTEM_NAMES[system]} | {name} | " + " | ".join(cells) + " |")
    out += ["", "Correct answers per category (mean ± std over the runs):", "",
            "| System | Model | " + " | ".join(cats) + " |", "|---|---|" + "---|" * len(cats)] + by_cat
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
    lines = ["# Experiment results (v2, 26 November 2026)", "",
             "Generated by `eval/summarize.py` from the files in `eval/results/`. Test split only. "
             "Haiku: three runs per combination (mean ± std); Qwen and Opus: one run. "
             "95% CI: bootstrap over the questions (2,000 draws).", "",
             "## E1 Accuracy: our question set (50 test questions)", ""] + accuracy_table("v2")
    lines += ["", "## E1 Accuracy: FinanceBench", "",
              "All models answer all 150 questions.", ""]
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
    lines += ["", "## E6 Update run: adding the 10-Qs filed since the last load", ""]
    for path in sorted(R.glob("update_*.csv")):
        rows = list(csv.DictReader(path.open()))
        lines += [f"**{path.stem}**", "", "| Step | Seconds |", "|---|---|"]
        lines += [f"| {r['step']} | {r['seconds']} |" for r in rows if r["step"] != "result"]
        lines += [f"| total | {sum(float(r['seconds']) for r in rows if r['step'] != 'result'):.0f} |", "",
                  next(r["detail"] for r in rows if r["step"] == "result"), ""]
    if (R / "filings_per_day.csv").exists():
        days = [int(r["filings"]) for r in csv.DictReader((R / "filings_per_day.csv").open())]
        lines += [f"10-Qs per filing day (2024-2026, {len(days)} days): median {sorted(days)[len(days) // 2]}, "
                  f"max {max(days)}, days over 300: {sum(d > 300 for d in days)}"]
    for path in sorted((R / "load").glob("*.json")):
        data = json.loads(path.read_text())
        lines += ["", f"## E4 HTTP load test: {data['name']}", "",
                  "| Users | Answers / s | Median ms | p95 ms | p99 ms | Error rate |", "|---|---|---|---|---|---|"]
        lines += [f"| {r['users']} | {r['per_second']} | {r['median_ms']} | {r['p95_ms']} | {r['p99_ms']} | {r['error_rate']:.2%} |"
                  for r in data["steps"]]
    if (R / "grader_agreement.json").exists():
        g = json.loads((R / "grader_agreement.json").read_text())
        lines += ["", "## Grader check", "",
                  f"Haiku judge vs {g['model']} on {g['n']} random LLM-graded answers: {g['agree']}/{g['n']} agree "
                  f"({100 * g['agree'] / g['n']:.0f}%), Cohen's kappa {g['kappa']}.", "",
                  "| judge \\ strong grader | " + " | ".join(g["confusion"]) + " |", "|---|---|---|---|"]
        lines += [f"| {a} | " + " | ".join(str(v) for v in row.values()) + " |" for a, row in g["confusion"].items()]
    (HERE / "RESULTS_v2.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
