"""LLM calls, in two tiers, every request traced to the dashboard.

System 1 (fast, SIEVEC_S1_MODEL, default "nimble"): category decisions.
System 2 (slow, SIEVEC_S2_MODEL, default "qwen3.8"): reading web results.
Both go to the Ollama-compatible endpoint at SIEVEC_LLM_URL. If it fails
and GUAVA_API_KEY is set, Guava's hosted LLM is the fallback.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Callable

import httpx

logger = logging.getLogger("sievec.llm")

LLM_URL = os.environ.get("SIEVEC_LLM_URL", "https://9ol.es/11434").rstrip("/")
MODELS = {
    1: os.environ.get("SIEVEC_S1_MODEL", "nimble"),
    2: os.environ.get("SIEVEC_S2_MODEL", "qwen3.8"),
}

# Which call a request belongs to, so the dashboard can file the trace.
current_call: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_call", default=None)
_trace_sink: Callable[[dict[str, Any]], None] | None = None


def set_trace_sink(fn: Callable[[dict[str, Any]], None]) -> None:
    global _trace_sink
    _trace_sink = fn


def trace(kind: str, purpose: str, request: Any, response: Any, started: float, **extra: Any) -> None:
    if _trace_sink is None:
        return
    _trace_sink({
        "id": uuid.uuid4().hex[:10],
        "ts": time.time(),
        "call_id": current_call.get(),
        "kind": kind,
        "purpose": purpose,
        "latency_ms": int((time.time() - started) * 1000),
        "request": request,
        "response": response,
        **extra,
    })


def config() -> dict[str, Any]:
    return {"endpoint": f"{LLM_URL}/api/chat", "systemone_endpoint": f"{LLM_URL}/v1/systemone",
            "system1_model": MODELS[1], "system2_model": MODELS[2],
            "guava_fallback": bool(os.environ.get("GUAVA_API_KEY"))}


def _parse(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def complete_json(prompt: str, schema: dict, *, system: int = 2, purpose: str = "llm") -> tuple[dict, str]:
    """Return (parsed JSON, model label). Raises if every backend fails."""
    shape = json.dumps(schema.get("properties", {}), separators=(",", ":"))
    payload = {
        "model": MODELS[system],
        "stream": False,
        "think": False,
        "format": "json",
        "options": {"temperature": 0},
        "messages": [{"role": "user", "content":
                      f"{prompt}\n\nReply with ONLY a JSON object with these keys (JSON Schema properties): {shape}"}],
    }
    started = time.time()
    try:
        r = httpx.post(f"{LLM_URL}/api/chat", json=payload, timeout=25.0 if system == 2 else 12.0)
        r.raise_for_status()
        body = r.json()
        out = _parse(body["message"]["content"])
        trace("llm", purpose, {"url": f"{LLM_URL}/api/chat", **payload}, body, started,
              system=system, model=MODELS[system])
        return out, MODELS[system]
    except Exception as exc:  # noqa: BLE001
        trace("llm", purpose, {"url": f"{LLM_URL}/api/chat", **payload}, {"error": str(exc)}, started,
              system=system, model=MODELS[system], error=True)
        logger.warning("%s (%s) failed: %s", MODELS[system], purpose, exc)
        if not os.environ.get("GUAVA_API_KEY"):
            raise

    started = time.time()
    from guava.helpers.llm import generate
    text = generate(prompt, json_schema=schema)
    trace("llm", purpose, {"backend": "guava.helpers.llm.generate", "prompt": prompt, "json_schema": schema},
          {"text": text}, started, system=system, model="guava")
    return _parse(text), "guava"


def warm() -> None:
    """Load the System One model so the first live call isn't the slow one."""
    try:
        systemone("Caller: hello", {"ok": {"type": "noul", "instructions": "Is the caller greeting?"}},
                  purpose="warm-up")
    except Exception:  # noqa: BLE001
        pass


def systemone(state: str, questions: dict[str, Any], purpose: str = "System One") -> dict[str, Any]:
    """Ollama System One (POST /v1/systemone): typed questions (choice / noul /
    score) about one piece of text, answered with probabilities in one call.
    https://docs.ollama.com/api/systemone"""
    body = {"model": MODELS[1], "state": state, "questions": questions, "keep_alive": "30m"}
    url = f"{LLM_URL}/v1/systemone"
    started = time.time()
    try:
        r = httpx.post(url, json=body, timeout=15.0)
        r.raise_for_status()
        out = r.json()
    except Exception as exc:  # noqa: BLE001
        trace("systemone", purpose, {"url": url, **body}, {"error": str(exc)}, started,
              system=1, model=MODELS[1], error=True)
        raise
    trace("systemone", purpose, {"url": url, **body}, out, started, system=1, model=MODELS[1])
    return out["answers"]
