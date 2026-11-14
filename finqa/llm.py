"""LLM backends behind one interface: parse() for structured output, text() for free text.

Default from the environment: FINQA_LLM (a preset name) and FINQA_MODEL (model override).
Switch at runtime with configure(llm, model), the CLI/eval flags --llm/--model, or the API's
"llm"/"model" fields. Presets: claude, chatgpt, qwen (= mlx Qwen2.5-7B), qwen-14b, mlx, ollama, openai.

  claude   (default)  Anthropic API, model FINQA_MODEL (default claude-opus-5-5).
                      Schema enforced server-side via structured outputs.
  mlx                 Local model on Apple Silicon via mlx-lm, model FINQA_MODEL
                      (default mlx-community/Qwen2.5-7B-Instruct-4bit).
  chatgpt             OpenAI API (api.openai.com), key in OPENAI_API_KEY, model FINQA_MODEL or
                      --model (default gpt-4.1). Uses the same "openai" backend as below.
  openai              Any OpenAI-compatible chat server (Ollama, LM Studio, vLLM, llama.cpp),
                      FINQA_OPENAI_URL (default http://localhost:11434/v1, i.e. Ollama),
                      model FINQA_MODEL (e.g. qwen2.5:7b-instruct), optional FINQA_OPENAI_KEY.

Local backends have no server-side schema enforcement, so the JSON schema is put in the prompt,
the reply is validated with the same Pydantic model, and one repair attempt is made on failure.
Whatever the backend, the workflow's own checks (allowed lists, citation ids) still apply.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache

from pydantic import BaseModel, ValidationError

from .config import LLM_EFFORT, LLM_MODEL

# Shorthands accepted by configure() / --llm.  name -> (backend, default model)
PRESETS = {
    "claude": ("claude", LLM_MODEL),
    "mlx": ("mlx", "mlx-community/Qwen2.5-7B-Instruct-4bit"),
    "qwen": ("mlx", "mlx-community/Qwen2.5-7B-Instruct-4bit"),        # local, Apple Silicon
    "qwen-14b": ("mlx", "mlx-community/Qwen2.5-14B-Instruct-4bit"),
    "openai": ("openai", "qwen2.5:7b-instruct"),                     # e.g. Ollama
    "ollama": ("openai", "qwen2.5:7b-instruct"),
    "chatgpt": ("openai", "gpt-4.1"),                                # OpenAI API
}

# Endpoint settings for presets on the OpenAI-compatible backend.
#   url: base URL, key_env: env var holding the API key, context: max input tokens, max_out: output cap
_LOCAL_SERVER = {"url": os.environ.get("FINQA_OPENAI_URL", "http://localhost:11434/v1"),
                 "key_env": "FINQA_OPENAI_KEY", "context": int(os.environ.get("FINQA_LOCAL_CONTEXT", "32768")),
                 "max_out": 2048}
ENDPOINTS = {
    "openai": _LOCAL_SERVER,
    "ollama": _LOCAL_SERVER,
    # gpt-4.1 has a ~1M-token context, so the full-10-Q baseline fits. Change if you pick another model.
    "chatgpt": {"url": os.environ.get("FINQA_CHATGPT_URL", "https://api.openai.com/v1"), "key_env": "OPENAI_API_KEY",
                "context": int(os.environ.get("FINQA_CHATGPT_CONTEXT", "1000000")), "max_out": 16000},
}

BACKEND = "claude"
MODEL = LLM_MODEL
OPENAI_URL = _LOCAL_SERVER["url"].rstrip("/")
OPENAI_KEY_ENV = _LOCAL_SERVER["key_env"]
LOCAL_CONTEXT = _LOCAL_SERVER["context"]   # used to refuse a too-long prompt, never to truncate it
MAX_OUT = _LOCAL_SERVER["max_out"]


def configure(llm: str | None = None, model: str | None = None) -> None:
    """Pick the backend (a PRESETS name) and optionally override its model.
    Defaults come from FINQA_LLM / FINQA_MODEL; call again at any time to switch."""
    global BACKEND, MODEL, OPENAI_URL, OPENAI_KEY_ENV, LOCAL_CONTEXT, MAX_OUT
    name = (llm or os.environ.get("FINQA_LLM", "claude")).lower()
    if name not in PRESETS:
        raise ValueError(f"unknown llm '{name}', choose from {sorted(PRESETS)}")
    BACKEND, default = PRESETS[name]
    # FINQA_MODEL only applies when the backend also comes from the environment, so that
    # e.g. `--llm qwen` is not combined with a Claude model id left in the shell.
    MODEL = model or (os.environ.get("FINQA_MODEL") if llm is None else None) or default
    ep = ENDPOINTS.get(name, _LOCAL_SERVER)
    OPENAI_URL, OPENAI_KEY_ENV = ep["url"].rstrip("/"), ep["key_env"]
    LOCAL_CONTEXT, MAX_OUT = ep["context"], ep["max_out"]
    _mlx_model.cache_clear() if "_mlx_model" in globals() else None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    seconds: float = 0.0
    log: list = field(default_factory=list)

    def add(self, name: str, tin: int, tout: int, seconds: float):
        self.input_tokens += tin
        self.output_tokens += tout
        self.calls += 1
        self.seconds += seconds
        self.log.append({"step": name, "backend": BACKEND, "in": tin, "out": tout, "s": round(seconds, 2)})


def model_name() -> str:
    return f"{BACKEND}:{MODEL}"


def context_limit() -> int | None:
    """Max input tokens for the active backend (None = effectively unlimited for our docs)."""
    return None if BACKEND == "claude" else LOCAL_CONTEXT


def available() -> bool:
    if os.environ.get("FINQA_OFFLINE") == "1":
        return False
    if BACKEND == "claude":
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return True
        return os.path.exists(os.path.expanduser("~/.config/anthropic"))
    if BACKEND == "mlx":
        try:
            import mlx_lm  # noqa: F401
            return True
        except ImportError:
            return False
    if BACKEND == "openai":
        if OPENAI_KEY_ENV == "OPENAI_API_KEY":  # hosted API: a key is required
            return bool(os.environ.get("OPENAI_API_KEY"))
        try:  # local server: is it running?
            import requests
            requests.get(f"{OPENAI_URL}/models", timeout=2, headers=_openai_headers())
            return True
        except Exception:
            return False
    raise ValueError(f"unknown FINQA_LLM backend: {BACKEND}")


# ============================================================================ public API
def parse(name: str, system: str, user: str, schema: type[BaseModel], usage: Usage,
          max_tokens: int = 4000) -> BaseModel:
    """Structured output validated against a Pydantic model. Returns the parsed instance."""
    if BACKEND == "claude":
        return _claude_parse(name, system, user, schema, usage, max_tokens)
    return _local_parse(name, system, user, schema, usage, max_tokens)


def text(name: str, system: str, user: str, usage: Usage, max_tokens: int = 16000,
         effort: str | None = None) -> str:
    """Plain text completion (used by the baseline)."""
    if BACKEND == "claude":
        return _claude_text(name, system, user, usage, max_tokens, effort)
    return _local_chat(name, system, user, usage, max_tokens)


# ============================================================================ Claude
_client = None


def _claude():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(timeout=120, max_retries=2)
    return _client


def _claude_parse(name, system, user, schema, usage, max_tokens):
    t = time.time()
    resp = _claude().messages.parse(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=schema,
        output_config={"effort": LLM_EFFORT},
    )
    usage.add(name, resp.usage.input_tokens, resp.usage.output_tokens, time.time() - t)
    if resp.stop_reason == "refusal":
        raise RuntimeError("model refused")
    return resp.parsed_output


def _claude_text(name, system, user, usage, max_tokens, effort):
    t = time.time()
    with _claude().messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort or LLM_EFFORT},
    ) as stream:
        resp = stream.get_final_message()
    usage.add(name, resp.usage.input_tokens, resp.usage.output_tokens, time.time() - t)
    if resp.stop_reason == "refusal":
        return "[refused]"
    return "".join(b.text for b in resp.content if b.type == "text")


# ============================================================================ local models
_JSON_INSTRUCTION = """

Reply with ONLY a JSON object (no prose, no markdown fences) of this shape:
{schema}"""


def compact_schema(schema: type[BaseModel]) -> str:
    """One-line type sketch of a Pydantic model, e.g. {"qtype": "numeric"|"both", "company": string|null}.
    ~4x fewer tokens than the full JSON Schema; the reply is still validated against the full model."""
    js = schema.model_json_schema()
    defs = js.get("$defs", {})

    def t(p: dict) -> str:
        if "$ref" in p:
            return t(defs[p["$ref"].split("/")[-1]])
        if "enum" in p:
            return "|".join(json.dumps(e) for e in p["enum"])
        if "anyOf" in p:
            return "|".join(t(x) for x in p["anyOf"])
        if p.get("type") == "array":
            return f"[{t(p.get('items', {}))}]"
        if p.get("type") == "object" and "properties" in p:
            return "{" + ", ".join(f'"{k}": {t(v)}' for k, v in p["properties"].items()) + "}"
        return p.get("type", "any")
    return t(js)


def _local_parse(name, system, user, schema, usage, max_tokens):
    sys_prompt = system + _JSON_INSTRUCTION.format(schema=compact_schema(schema))
    raw = _local_chat(name, sys_prompt, user, usage, max_tokens, json_mode=True)
    try:
        return schema.model_validate_json(_extract_json(raw))
    except (ValidationError, ValueError) as e:
        # one repair round: show the model its own output and the validation error
        fix = (f"{user}\n\nYour previous reply was invalid:\n{raw[:2000]}\n\nError: {str(e)[:500]}\n"
               "Return only the corrected JSON object.")
        raw = _local_chat(name + "_repair", sys_prompt, fix, usage, max_tokens, json_mode=True)
        return schema.model_validate_json(_extract_json(raw))


def _extract_json(raw: str) -> str:
    """Strip <think> blocks / code fences and return the outermost {...} object."""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    raw = re.sub(r"```(?:json)?", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    return raw[start:end + 1]


def _local_chat(name, system, user, usage, max_tokens, json_mode: bool = False) -> str:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    t = time.time()
    if BACKEND == "mlx":
        out, tin, tout = _mlx_generate(messages, max_tokens)
    elif BACKEND == "openai":
        out, tin, tout = _openai_generate(messages, max_tokens, json_mode)
    else:
        raise ValueError(f"unknown FINQA_LLM backend: {BACKEND}")
    usage.add(name, tin, tout, time.time() - t)
    return out


@lru_cache(maxsize=1)
def _mlx_model():
    from mlx_lm import load
    return load(MODEL)


def _mlx_generate(messages, max_tokens):
    from mlx_lm import generate
    model, tok = _mlx_model()
    prompt = tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
    tin = len(tok.encode(prompt))
    if tin > LOCAL_CONTEXT:
        raise ValueError(f"prompt is {tin} tokens, over the local context window ({LOCAL_CONTEXT})")
    out = generate(model, tok, prompt=prompt, max_tokens=min(max_tokens, 2048), verbose=False)
    return out, tin, len(tok.encode(out))


def _openai_headers() -> dict:
    key = os.environ.get(OPENAI_KEY_ENV)
    return {"Authorization": f"Bearer {key}"} if key else {}


def _openai_generate(messages, max_tokens, json_mode):
    """POST /chat/completions. Works for the OpenAI API (ChatGPT models) and local servers.

    Servers and models differ in which parameters they accept (e.g. OpenAI reasoning models want
    max_completion_tokens and no temperature; some local servers have no JSON mode), so on a 400
    that names a parameter, that parameter is adjusted and the request is retried."""
    import requests
    body = {"model": MODEL, "messages": messages, "max_tokens": min(max_tokens, MAX_OUT), "temperature": 0}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    for _ in range(4):
        r = requests.post(f"{OPENAI_URL}/chat/completions", json=body, headers=_openai_headers(), timeout=600)
        if r.status_code != 400:
            break
        err = r.text.lower()
        if "max_tokens" in err and "max_tokens" in body:
            body["max_completion_tokens"] = body.pop("max_tokens")
        elif "temperature" in err and "temperature" in body:
            body.pop("temperature")
        elif "response_format" in body:
            body.pop("response_format")
        else:
            break
    r.raise_for_status()
    d = r.json()
    u = d.get("usage") or {}
    return d["choices"][0]["message"]["content"] or "", u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


configure()  # initialise from environment
