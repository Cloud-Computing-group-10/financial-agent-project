"""The HTTP API with a stand-in for the LLM, for the HTTP load test (eval/http_load_test.py).

  STUB_LLM_SECONDS=0 uvicorn eval.stub_api:app --host 0.0.0.0 --port 8080 --workers 4

It is the real API (api.py) and the real workflow, except for the LLM steps, as in eval/load_test.py:
the parse comes from the question's known fields, the "why" claims are the top passages, and the
writer is off. With STUB_LLM_SECONDS > 0 the parse step also waits that long, the way a request
waits for a real LLM. That shows how the server copes with many slow requests at once, without
paying for LLM calls. Only questions of the v2 test set are known.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import anyio.to_thread

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from load_test import known_parse, load_questions  # noqa: E402

import api  # noqa: E402
from finqa import workflow  # noqa: E402

DELAY = float(os.environ.get("STUB_LLM_SECONDS", "0"))
THREADS = int(os.environ.get("STUB_THREADS", "1000"))  # requests one worker may serve at once

_parses = {q["question"]: known_parse(q) for q in load_questions()}


def _parse(question, usage):
    if DELAY:
        time.sleep(DELAY)  # waiting for an LLM costs no CPU, like this sleep
    return _parses[question]


workflow.parse_question = _parse
workflow.compose_explanation = lambda question, passages, num, usage: [
    {"text": p["text"][:200], "chunk_ids": [p["chunk_id"]]} for p in passages[:2]]
workflow.write_enabled = lambda: False

app = api.app


@app.on_event("startup")
async def _more_threads() -> None:
    # FastAPI runs each request in a thread pool of 40 threads by default; a request that waits for
    # an LLM keeps its thread, so 40 would cap the number of users served at once.
    anyio.to_thread.current_default_thread_limiter().total_tokens = THREADS
