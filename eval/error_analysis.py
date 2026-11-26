"""FinanceBench error analysis: how the answers of workflow version 1 (A) and version 2 (A2) differ.

  python eval/error_analysis.py

Each question gets the majority verdict over the three Haiku runs of each version (correct,
incorrect or declined). It prints the transition table (v1 verdict -> v2 verdict) and, for the
causes assigned by hand to the v1 failures of run 1 (eval/fb_error_causes.json), how many of them
v2 answers correctly.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
R = HERE / "results" / "financebench"
MODEL = "claude_claude-haiku-5-5"


def majority(system: str) -> dict[str, str]:
    runs = [R / f"graded_{system}__{MODEL}{suffix}.jsonl" for suffix in ("", "__r2", "__r3")]
    votes: dict[str, list[str]] = {}
    for path in runs:
        for line in path.read_text().splitlines():
            r = json.loads(line)
            votes.setdefault(r["id"], []).append(r["verdict"])
    return {q: Counter(v).most_common(1)[0][0] for q, v in votes.items()}


def main() -> None:
    v1, v2 = majority("A"), majority("A2")
    labels = ["correct", "incorrect", "declined"]
    print("v1 \\ v2 | " + " | ".join(labels))
    for a in labels:
        print(f"{a} | " + " | ".join(str(sum(v1[q] == a and v2[q] == b for q in v1)) for b in labels))
    causes = json.loads((HERE / "fb_error_causes.json").read_text())
    print("\ncause (v1 run 1) | n | v2 correct | v2 incorrect | v2 declined")
    out = {}
    for cause, ids in causes.items():
        if cause.startswith("_"):
            continue
        c = Counter(v2[q] for q in ids)
        out[cause] = {"n": len(ids), **{k: c.get(k, 0) for k in labels}}
        print(f"{cause} | {len(ids)} | {c['correct']} | {c['incorrect']} | {c['declined']}")
    transitions = {a: {b: sum(v1[q] == a and v2[q] == b for q in v1) for b in labels} for a in labels}
    (HERE / "results" / "fb_error_analysis.json").write_text(json.dumps({"transitions": transitions, "causes": out}, indent=1))


if __name__ == "__main__":
    main()
