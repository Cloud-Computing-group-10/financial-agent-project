"""Passage search over filing chunks: BM25 (keyword baseline), semantic (bge-small), hybrid (RRF).

Scores are returned by the tool itself; the workflow's confidence comes from these scores,
never from the LLM's self-report (plan section 9).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

import numpy as np
from rank_bm25 import BM25Okapi

from .config import EMB_IDS_PATH, EMB_PATH, EMBED_MODEL
from .db import connect

QUERY_PREFIX = ""  # MiniLM needs no query instruction (bge models would)
_TOKEN = re.compile(r"[a-z0-9]+(?:[.'’-][a-z0-9]+)*")
STOP = set("""a an the of to in on for and or by with is are was were be been this that these those
it its as at from than which what why how did does do our we us their they during period quarter
compared versus vs""".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOP]


@lru_cache(maxsize=1)
def _embedder():
    """Sentence-transformers model run via plain transformers (mean pooling).

    Loads from the local Hugging Face cache only (no network) unless FINQA_ALLOW_DOWNLOAD=1.
    """
    import os
    import torch
    from transformers import AutoModel, AutoTokenizer
    local = os.environ.get("FINQA_ALLOW_DOWNLOAD") != "1"
    tok = AutoTokenizer.from_pretrained(EMBED_MODEL, local_files_only=local)
    model = AutoModel.from_pretrained(EMBED_MODEL, local_files_only=local).eval()
    return tok, model, torch


def embed(texts: list[str], batch_size: int = 32) -> np.ndarray:
    tok, model, torch = _embedder()
    out = []
    with torch.no_grad():
        for i in range(0, len(texts), batch_size):
            enc = tok(texts[i:i + batch_size], padding=True, truncation=True, max_length=256,
                      return_tensors="pt")
            hidden = model(**enc).last_hidden_state
            mask = enc["attention_mask"].unsqueeze(-1).float()
            out.append(((hidden * mask).sum(1) / mask.sum(1)).numpy())
    vecs = np.concatenate(out).astype(np.float32)
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def _chunk_doc_text(row) -> str:
    return f"{row['heading']}\n{row['text']}" if row["heading"] else row["text"]


def build_index(conn) -> None:
    rows = conn.execute("SELECT chunk_id, heading, text FROM chunks ORDER BY chunk_id").fetchall()
    print(f"embedding {len(rows)} chunks with {EMBED_MODEL} ...")
    vecs = embed([_chunk_doc_text(r) for r in rows])
    np.save(EMB_PATH, vecs)
    EMB_IDS_PATH.write_text(json.dumps([r["chunk_id"] for r in rows]))
    _Index.load.cache_clear()


class _Index:
    def __init__(self):
        conn = connect()
        rows = conn.execute(
            """SELECT c.chunk_id, c.report_id, c.section, c.heading, c.ordinal, c.text,
                      d.accession, d.title, d.period, d.url
               FROM chunks c JOIN documents d USING(report_id) ORDER BY c.chunk_id""").fetchall()
        conn.close()
        self.rows = [dict(r) for r in rows]
        self.pos = {r["chunk_id"]: i for i, r in enumerate(self.rows)}
        ids = json.loads(EMB_IDS_PATH.read_text())
        vecs = np.load(EMB_PATH)
        order = [ids.index(r["chunk_id"]) for r in self.rows] if ids != [r["chunk_id"] for r in self.rows] else None
        self.vecs = vecs[order] if order else vecs
        self.by_report: dict[int, list[int]] = {}
        for i, r in enumerate(self.rows):
            self.by_report.setdefault(r["report_id"], []).append(i)
        self._bm25: dict[tuple, BM25Okapi] = {}

    @staticmethod
    @lru_cache(maxsize=1)
    def load() -> "_Index":
        return _Index()

    def candidates(self, report_ids, section: str | None = None) -> list[int]:
        idx = list(range(len(self.rows))) if not report_ids else \
            [i for rid in report_ids for i in self.by_report.get(rid, [])]
        if section:
            idx = [i for i in idx if section.lower() in (self.rows[i]["section"] or "").lower()]
        return idx

    def bm25(self, cand: list[int]) -> BM25Okapi:
        key = tuple(cand)
        if key not in self._bm25:
            self._bm25[key] = BM25Okapi([tokenize(_chunk_doc_text(self.rows[i])) for i in cand])
        return self._bm25[key]


def _semantic(idx: _Index, query: str, cand: list[int]) -> list[tuple[int, float]]:
    q = embed([QUERY_PREFIX + query])[0]
    scores = idx.vecs[cand] @ q
    return sorted(zip(cand, scores.tolist()), key=lambda x: -x[1])


def _bm25(idx: _Index, query: str, cand: list[int]) -> list[tuple[int, float]]:
    scores = idx.bm25(cand).get_scores(tokenize(query))
    return sorted(zip(cand, scores.tolist()), key=lambda x: -x[1])


def _hybrid(idx: _Index, query: str, cand: list[int], k_rrf: int = 60) -> list[tuple[int, float]]:
    fused: dict[int, float] = {}
    for ranked in (_semantic(idx, query, cand), _bm25(idx, query, cand)):
        for rank, (i, _) in enumerate(ranked):
            fused[i] = fused.get(i, 0.0) + 1.0 / (k_rrf + rank + 1)
    return sorted(fused.items(), key=lambda x: -x[1])


METHODS = {"semantic": _semantic, "bm25": _bm25, "hybrid": _hybrid}


MDA = "Part I Item 2."  # Management's Discussion and Analysis


def search(query: str, report_ids: list[int] | None = None, method: str = "semantic",
           k: int = 5, section: str | None = None) -> list[dict]:
    """Return top-k passages with score + source metadata (from the index, not the model).

    section: optional substring filter on the section label (e.g. MDA)."""
    idx = _Index.load()
    cand = idx.candidates(report_ids, section)
    if not cand:
        return []
    ranked = METHODS[method](idx, query, cand)[:k]
    out = []
    for i, score in ranked:
        r = idx.rows[i]
        out.append({**r, "score": round(float(score), 4), "method": method,
                    "locator": f"{r['title']} › {r['section']}" + (f" › {r['heading']}" if r["heading"] else "")
                               + f" (chunk {r['chunk_id']})"})
    return out
