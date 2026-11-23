"""Retrieval experiment (plan 16.6): BM25 vs semantic vs hybrid search, no LLM.

  python eval/retrieval_eval.py                 both sets, all methods
  python eval/retrieval_eval.py --set financebench --terms

The search runs over the passages of the correct filing, as in the workflow; only the method changes.
  v2 "why" questions   a hit is a passage that contains one of the question's evidence phrases.
                       Searched in MD&A (the workflow's first round) and in the whole filing.
  FinanceBench         a hit is a passage from one of the question's evidence pages.
Metrics: hit@1/3/5/10 (a relevant passage among the top k) and MRR@10. For the "why" questions it
also prints the top score of each search, to calibrate the evidence thresholds on the dev split.
--terms adds the parser's search terms to the query (one LLM call per question, as in document QA).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from finqa.config import MDA_SECTION  # noqa: E402
from finqa.db import connect  # noqa: E402
from finqa.search import METHODS, search  # noqa: E402

HERE = Path(__file__).resolve().parent
KS = (1, 3, 5, 10)


def norm(text: str) -> str:
    return " ".join(text.lower().split())


def cases(set_name: str, terms: bool) -> list[dict]:
    """(question, filing, section filter, relevance test) for every question of the set."""
    out = []
    if set_name == "v2":
        key = {r["id"]: r for line in (HERE / "answer_key_v2.jsonl").open() for r in [json.loads(line)]}
        for line in (HERE / "questions_v2.jsonl").open():
            q = json.loads(line)
            if q["category"] != "why":
                continue
            phrases = [norm(e) for e in q["evidence"]]
            relevant = (lambda p, ph=phrases: any(e in norm(p["text"]) for e in ph))
            for scope in ("mda", "all"):
                out.append({"id": q["id"], "split": q["split"], "scope": scope, "query": q["question"],
                            "accession": key[q["id"]]["accession"],
                            "section": MDA_SECTION if scope == "mda" else None, "relevant": relevant})
    else:
        for line in (HERE.parent / "data" / "financebench" / "financebench_open_source.jsonl").open():
            q = json.loads(line)
            pages = {f"page {e['evidence_page_num'] + 1}" for e in q["evidence"]}  # page numbers are 0-based
            out.append({"id": q["financebench_id"], "split": "test", "scope": "document", "query": q["question"],
                        "accession": f"financebench:{q['doc_name']}", "section": None,
                        "relevant": (lambda p, pg=pages: p["section"] in pg)})
    if terms:
        from finqa import llm
        from finqa.parse import parse_question
        for c in out:
            c["query"] = f"{parse_question(c['query'], llm.Usage()).search_terms or ''}\n{c['query']}"
    return out


def evaluate(conn, set_name: str, terms: bool) -> dict:
    report_ids = {}
    results = {}
    data = cases(set_name, terms)
    for method in METHODS:
        rows = []
        for c in data:
            if c["accession"] not in report_ids:
                doc = conn.execute("SELECT report_id FROM documents WHERE accession = %s", (c["accession"],)).fetchone()
                report_ids[c["accession"]] = doc and doc["report_id"]
            rid = report_ids[c["accession"]]
            t = time.perf_counter()
            hits = search(conn, c["query"], [rid], method=method, k=max(KS), section=c["section"]) if rid else []
            seconds = time.perf_counter() - t
            ranks = [i for i, h in enumerate(hits, 1) if c["relevant"](h)]
            rows.append({"id": c["id"], "split": c["split"], "scope": c["scope"], "first_hit": ranks[0] if ranks else None,
                         "top_score": hits[0]["score"] if hits else None, "seconds": round(seconds, 4)})
        results[method] = rows
    return results


def table(results: dict, split: str | None = None) -> str:
    lines = ["| method | scope | n | " + " | ".join(f"hit@{k}" for k in KS) + " | MRR@10 | median ms |",
             "|---|---|---|" + "---|" * (len(KS) + 2)]
    for method, rows in results.items():
        for scope in sorted({r["scope"] for r in rows}):
            sel = [r for r in rows if r["scope"] == scope and (split is None or r["split"] == split)]
            if not sel:
                continue
            hit = [sum(r["first_hit"] is not None and r["first_hit"] <= k for r in sel) / len(sel) for k in KS]
            mrr = sum(1 / r["first_hit"] for r in sel if r["first_hit"]) / len(sel)
            ms = sorted(r["seconds"] for r in sel)[len(sel) // 2] * 1000
            lines.append(f"| {method} | {scope} | {len(sel)} | " + " | ".join(f"{h:.2f}" for h in hit)
                         + f" | {mrr:.2f} | {ms:.0f} |")
    return "\n".join(lines)


def main() -> None:
    load_dotenv(HERE.parent / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", choices=["v2", "financebench"], action="append")
    ap.add_argument("--terms", action="store_true", help="add the parser's search terms to the query (LLM)")
    args = ap.parse_args()
    out_dir = HERE / "results" / "retrieval"
    out_dir.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        for set_name in args.set or ["v2", "financebench"]:
            results = evaluate(conn, set_name, args.terms)
            suffix = "_terms" if args.terms else ""
            (out_dir / f"{set_name}{suffix}.json").write_text(json.dumps(results, indent=1))
            print(f"\n## {set_name}{suffix}\n")
            if set_name == "v2":
                for split in ("dev", "test"):
                    print(f"{split}:\n{table(results, split)}\n")
                print("Top score per search (dev, MD&A), to calibrate THRESHOLDS: relevant first hit vs none")
                for method, rows in results.items():
                    dev = [r for r in rows if r["split"] == "dev" and r["scope"] == "mda"]
                    print(f"  {method}: " + ", ".join(f"{r['id']}={r['top_score']}{'' if r['first_hit'] else '(miss)'}" for r in dev))
            else:
                print(table(results))


if __name__ == "__main__":
    main()
