"""Experiments of plan section 16: systems A/B/C x models, on our question set (v2) and FinanceBench.

  python eval/run_experiments.py run --set v2 --system A --llm claude --model claude-haiku-5-5
  python eval/run_experiments.py grade --set v2
  python eval/run_experiments.py summary --set v2

Systems:
  A  our workflow (finqa.workflow.answer); on FinanceBench it is given the document
  A0 ablation: the same workflow with the template answer instead of the LLM-written one
  B  full-document baseline: the whole correct filing + the question
  C  simple RAG baseline: top passages of the correct filing + the question
B and C always get the correct filing (from the answer key / FinanceBench), which favours them.

Every answer is appended to eval/results/<set>/<system>__<model>.jsonl as soon as it is done, so a
stopped run resumes without paying twice. Grading is a separate step:
  * numbers (v2 lookup/change/paraphrase/latest): the gold value must appear in the answer text,
    to the precision the answer states; for a change, the percentage (+-0.15 points) and either the
    new value or the absolute change (only the % if the question asks for a percentage).
  * v2 "why" and every FinanceBench answer: an LLM grader (eval/judge.py) compares the answer with
    the gold reason / answer and labels it correct, incorrect or declined.
  * v2 "decline" questions: system A must return abstain/clarify; for B and C the grader decides
    whether the answer declines.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
FINANCEBENCH = HERE.parent / "data" / "financebench" / "financebench_open_source.jsonl"
NUMERIC = {"lookup", "change", "paraphrase", "latest"}


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


# ---------------------------------------------------------------------------- data sets
def questions(name: str) -> list[dict]:
    """Questions with what each system needs: the text, the correct filing, and the gold answer."""
    if name == "v2":
        key = {r["id"]: r for r in load(HERE / "answer_key_v2.jsonl")}
        return [{**q, "accession": key[q["id"]].get("accession"), "gold": key[q["id"]]}
                for q in load(HERE / "questions_v2.jsonl")]
    return [{"id": q["financebench_id"], "split": "test", "category": q["question_type"], "question": q["question"],
             "accession": f"financebench:{q['doc_name']}", "gold": {"answer": q["answer"], "justification": q["justification"]}}
            for q in load(FINANCEBENCH)]


# ---------------------------------------------------------------------------- running
def ask(system: str, q: dict, set_name: str) -> dict:
    from finqa.baseline import baseline_answer
    from finqa.rag_baseline import rag_answer
    from finqa.workflow import answer
    if system in ("A", "A0"):
        return answer(q["question"], document=q["accession"] if set_name == "financebench" else None,
                      writer=system == "A")
    if not q.get("accession"):  # questions without a filing (unknown company, no company named)
        return {"status": "no_document", "answer": "", "usage": {}, "latency_s": 0}
    if system == "B":
        return baseline_answer(q["question"], accession=q["accession"])
    return rag_answer(q["question"], q["accession"])


def result_path(set_name: str, system: str, model: str) -> Path:
    return RESULTS / set_name / f"{system}__{model.replace('/', '_')}.jsonl"


def sample_ids(set_name: str, n: int) -> set[str]:
    """A fixed random sample of the set's questions (seed 0), the same for every system and model."""
    import random
    ids = sorted(q["id"] for q in questions(set_name))
    return set(random.Random(0).sample(ids, n))


def run(set_name: str, system: str, split: str | None, limit: int | None, sample: int | None = None) -> None:
    from finqa import llm
    path = result_path(set_name, system, llm.model_name())
    path.parent.mkdir(parents=True, exist_ok=True)
    done = {r["id"] for r in load(path)}
    chosen = sample_ids(set_name, sample) if sample else None
    todo = [q for q in questions(set_name) if q["id"] not in done and (not split or q["split"] == split)
            and (chosen is None or q["id"] in chosen)][:limit]
    print(f"{system} / {llm.model_name()} / {set_name}: {len(done)} done, {len(todo)} to run -> {path}")
    for q in todo:
        try:
            res = ask(system, q, set_name)
        except Exception as e:  # API error (credits, rate limit, network): stop, keep what is done
            print(f"  ! stopped at {q['id']}: {str(e)[:200]}\n  run the same command again to resume")
            return
        u = res.get("usage", {})
        row = {"id": q["id"], "system": system, "model": llm.model_name(), "status": res.get("status"),
               "answer": res.get("answer", ""), "route": res.get("trace", {}).get("route"),
               "flags": res.get("flags", []), "citations": [c.get("text", "")[:300] for c in res.get("citations", [])],
               "writer": res.get("trace", {}).get("writer"),
               "timings": res.get("trace", {}).get("timings"),
               "latency_s": res.get("latency_s"), "input_tokens": u.get("input_tokens", 0),
               "output_tokens": u.get("output_tokens", 0), "llm_calls": u.get("llm_calls", 0)}
        with path.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        print(f"  {q['id']:<24} {str(row['status']):<16} {row['latency_s']}s  {row['answer'][:70]!r}")


# ---------------------------------------------------------------------------- grading
_NUMBER = re.compile(r"(\()?\$?\s?(\d[\d,]*(?:\.(\d+))?)\s*(\))?\s*(billion|million|bn|mn)?", re.I)


def value_in(text: str, gold: float) -> bool:
    """Gold value stated in the text, to the precision the text uses ("$8.3 billion" ~ 8,325 million)."""
    for m in _NUMBER.finditer(text):
        v = float(m.group(2).replace(",", ""))
        decimals = len(m.group(3) or "")
        scales = [{"billion": 1e9, "bn": 1e9, "million": 1e6, "mn": 1e6}[m.group(5).lower()]] if m.group(5) else [1e6, 1]
        for scale in scales:  # a bare number is either millions (tables) or dollars (EPS)
            half_unit = 0.5 * 10 ** -decimals * scale
            if abs(v * scale - abs(gold)) <= max(half_unit, 1e-9 * abs(gold)) + 1e-9:
                return True
    return False


def pct_in(text: str, gold_pct: float) -> bool:
    for p in re.findall(r"(\d+(?:\.(\d+))?)\s*(?:%|percent)", text):
        if abs(float(p[0]) - abs(gold_pct)) <= max(0.15, 0.5 * 10 ** -len(p[1])) + 1e-9:
            return True
    return False


def grade_row(q: dict, row: dict, judge) -> dict:
    gold, cat, text = q["gold"], q["category"], row["answer"] or ""
    declined_status = row["status"] in ("abstain", "clarify", "no_document")
    if q.get("expect") in ("decline", "clarify"):
        if row["system"] in ("A", "A0") or declined_status:
            return {"verdict": "declined" if declined_status else "answered", "correct": declined_status}
        v = judge.grade(q["question"], f"The question must be declined: {gold['decline_reason']}.", text)
        return {"verdict": v["verdict"], "correct": v["verdict"] == "declined", "reason": v["reason"]}
    if declined_status:
        return {"verdict": "declined", "correct": False}
    if cat in NUMERIC:
        if gold.get("pct_change") is not None:  # a change: the %, plus the new value or the change itself
            ok = pct_in(text, gold["pct_change"])  # unless the question asks only for the percentage
            if not re.search(r"percent", q["question"], re.I):
                ok = ok and (value_in(text, gold["value"]) or value_in(text, gold["abs_change"]))
        else:
            ok = gold.get("value") is not None and value_in(text, gold["value"])
        return {"verdict": "correct" if ok else "incorrect", "correct": ok}
    reference = gold["reason"] if "reason" in gold else f"{gold['answer']}\nJustification: {gold['justification']}"
    v = judge.grade(q["question"], reference, text)
    return {"verdict": v["verdict"], "correct": v["verdict"] == "correct", "reason": v["reason"]}


def grade(set_name: str) -> None:
    from judge import Judge
    judge = Judge()
    qs = {q["id"]: q for q in questions(set_name)}
    for path in sorted((RESULTS / set_name).glob("*.jsonl")):
        if path.name.startswith("graded_"):
            continue
        out = RESULTS / set_name / f"graded_{path.name}"
        graded = {r["id"]: r for r in load(out)}
        rows = load(path)
        new = [r for r in rows if r["id"] not in graded]
        for r in new:
            graded[r["id"]] = {**r, **grade_row(qs[r["id"]], r, judge)}
        for r in rows:  # number grades are code: recompute them so a rule change applies everywhere
            if r["id"] in graded and qs[r["id"]]["category"] in NUMERIC and qs[r["id"]].get("expect") not in ("decline", "clarify"):
                graded[r["id"]] = {**r, **grade_row(qs[r["id"]], r, judge)}
        out.write_text("".join(json.dumps(graded[r["id"]]) + "\n" for r in rows if r["id"] in graded))
        print(f"{path.name}: graded {len(new)} new answers")


def agreement(set_names: list[str], model: str, n: int = 60, seed: int = 0) -> None:
    """Regrade a random sample of LLM-graded answers with a stronger model; print the agreement and
    write the sample (question, reference, answer, both grades) for a person to check by hand."""
    import csv
    import random
    from judge import Judge
    strong = Judge(model)
    pool = []
    for set_name in set_names:
        qs = {q["id"]: q for q in questions(set_name)}
        for path in sorted((RESULTS / set_name).glob("graded_*.jsonl")):
            for r in load(path):
                if "reason" in r:  # graded by the LLM grader
                    pool.append((set_name, qs[r["id"]], r))
    sample = random.Random(seed).sample(pool, min(n, len(pool)))
    rows, same = [], 0
    for set_name, q, r in sample:
        gold = q["gold"]
        if q.get("expect") in ("decline", "clarify"):
            reference = f"The question must be declined: {gold['decline_reason']}."
        elif "reason" in gold:
            reference = gold["reason"]
        else:
            reference = f"{gold['answer']}\nJustification: {gold['justification']}"
        v = strong.grade(q["question"], reference, r["answer"])
        same += v["verdict"] == r["verdict"]
        rows.append({"set": set_name, "id": r["id"], "system": r["system"], "model": r["model"],
                     "question": q["question"], "reference": reference, "answer": r["answer"],
                     "grader": r["verdict"], "strong_grader": v["verdict"], "human": ""})
    out = RESULTS / "grader_check.csv"
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"grader vs {model}: {same}/{len(rows)} agree -> {out} (fill in the 'human' column)")


# ---------------------------------------------------------------------------- summary
def summary(set_name: str) -> None:
    qs = {q["id"]: q for q in questions(set_name)}
    print("| system | model | n | accuracy | declined (should) | declined (should not) | median s | tokens in/out |")
    print("|---|---|---|---|---|---|---|---|")
    for path in sorted((RESULTS / set_name).glob("graded_*.jsonl")):
        rows = [r for r in load(path) if qs[r["id"]]["split"] == "test"]
        if not rows:
            continue
        should = [r for r in rows if qs[r["id"]].get("expect") in ("decline", "clarify")]
        answerable = [r for r in rows if r not in should]
        lat = sorted(r["latency_s"] or 0 for r in rows)
        print(f"| {rows[0]['system']} | {rows[0]['model']} | {len(rows)} | "
              f"{sum(r['correct'] for r in rows)}/{len(rows)} | "
              f"{sum(r['verdict'] == 'declined' for r in should)}/{len(should)} | "
              f"{sum(r['verdict'] == 'declined' for r in answerable)}/{len(answerable)} | "
              f"{lat[len(lat) // 2]:.1f} | {sum(r['input_tokens'] for r in rows) // len(rows)}/"
              f"{sum(r['output_tokens'] for r in rows) // len(rows)} |")
    for path in sorted((RESULTS / set_name).glob("graded_*.jsonl")):
        rows = [r for r in load(path) if qs[r["id"]]["split"] == "test"]
        by_cat: dict[str, list[bool]] = {}
        for r in rows:
            by_cat.setdefault(qs[r["id"]]["category"], []).append(r["correct"])
        print(path.name.removeprefix("graded_").removesuffix(".jsonl"), ":",
              ", ".join(f"{c} {sum(v)}/{len(v)}" for c, v in sorted(by_cat.items())))


def main() -> None:
    load_dotenv(HERE.parent / ".env", override=True)  # the project key in .env, not one left in the shell
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["run", "grade", "summary", "agreement"])
    ap.add_argument("--set", default="v2", choices=["v2", "financebench"])
    ap.add_argument("--system", choices=["A", "A0", "B", "C"])
    ap.add_argument("--llm", help="LLM preset (claude, hkust, qwen, ...)")
    ap.add_argument("--model", help="model id, e.g. claude-haiku-5-5")
    ap.add_argument("--split", choices=["test", "dev"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--sample", type=int, help="only a fixed random sample of N questions (seed 0)")
    args = ap.parse_args()
    if args.step == "run":
        from finqa import llm
        llm.configure(args.llm, args.model)
        run(args.set, args.system, args.split, args.limit, args.sample)
    elif args.step == "grade":
        grade(args.set)
    elif args.step == "agreement":
        agreement(["v2", "financebench"], args.model or "claude-sonnet-5-5")
    else:
        summary(args.set)


if __name__ == "__main__":
    main()
