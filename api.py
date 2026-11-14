"""HTTP API for container / cloud deployment (plan 15.6).

  uvicorn api:app --host 0.0.0.0 --port 8080
  curl -s localhost:8080/ask -H 'content-type: application/json' -d '{"question": "Why did 3M sales drop in Q2 2023?"}'
"""
from __future__ import annotations

import threading
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from finqa import llm
from finqa.workflow import answer

app = FastAPI(title="10-Q cited QA", version="0.1.0")


class AskRequest(BaseModel):
    question: str
    method: Literal["semantic", "bm25", "hybrid"] = "semantic"
    llm: str | None = None    # preset: claude | chatgpt | qwen | qwen-14b | mlx | ollama | openai
    model: str | None = None  # override the preset's model id


@app.get("/health")
def health():
    return {"ok": True, "llm": llm.model_name(), "llm_available": llm.available(),
            "presets": sorted(llm.PRESETS)}


_lock = threading.Lock()  # the backend choice is process-global; serialize switching requests


@app.post("/ask")
def ask(req: AskRequest):
    if req.llm or req.model:
        with _lock:
            llm.configure(req.llm, req.model)
            try:
                return answer(req.question, method=req.method)
            finally:
                llm.configure()  # back to the server default
    return answer(req.question, method=req.method)
