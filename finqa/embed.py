"""Passage embeddings: MiniLM (384 dimensions) through fastembed, on the CPU.

Used by the pilot ingest, by the Spark text job and by the search (to embed the question), so
passages and questions are always embedded by the same model.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np

from .config import EMBED_MODEL


@lru_cache(maxsize=None)
def _model(threads: int | None):
    from fastembed import TextEmbedding
    return TextEmbedding(EMBED_MODEL, threads=threads)


def embed(texts: list[str], threads: int | None = None) -> np.ndarray:
    """One normalised vector per text. threads=None uses every core; the Spark job passes 1,
    because Spark already runs one task per core. Small batches keep memory low (a batch of 256
    passages needs about 1.5 GB) and are just as fast."""
    return np.array(list(_model(threads).embed(texts, batch_size=32)), dtype=np.float32)


def passage_text(heading: str | None, text: str) -> str:
    """What gets embedded and keyword-indexed: the heading path gives the passage its context."""
    return f"{heading}\n{text}" if heading else text
