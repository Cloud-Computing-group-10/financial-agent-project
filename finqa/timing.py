"""Step timings for the latency breakdown (plan 16.8): trace["timings"][step] = seconds."""
from __future__ import annotations

import time
from contextlib import contextmanager


@contextmanager
def timed(trace: dict, step: str):
    """Add the time spent in the block to trace["timings"][step] (steps can repeat, e.g. search rounds)."""
    start = time.perf_counter()
    try:
        yield
    finally:
        timings = trace.setdefault("timings", {})
        timings[step] = round(timings.get(step, 0.0) + time.perf_counter() - start, 4)
