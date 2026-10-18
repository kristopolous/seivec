"""Past calls: reload them after a restart, and fill transcripts from Guava.

Guava keeps its own transcript and recording of every real call
(GET /v1/conversations/{id}/transcript and /recording). We use those when
our live capture is empty, e.g. the dashboard was restarted mid-call.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import httpx

from .bus import CALL_LOG, MAX_CALLS, bus

logger = logging.getLogger("sievec.history")

API = os.environ.get("GUAVA_API_URL", "https://app.goguava.ai").rstrip("/")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ.get('GUAVA_API_KEY', '')}"}


def load_past_calls() -> int:
    """Put the most recent logged calls back on the dashboard."""
    if not CALL_LOG.exists():
        return 0
    latest: dict[str, dict] = {}
    for line in CALL_LOG.read_text().splitlines():
        if line.strip():
            try:
                rec = json.loads(line)
                latest[rec["id"]] = rec
            except (json.JSONDecodeError, KeyError):
                continue
    recent = sorted(latest.values(), key=lambda c: c["started_at"])[-MAX_CALLS:]
    for c in recent:
        tag_emergency(c)
    with bus._lock:
        for c in recent:
            bus.calls.setdefault(c["id"], c)
    return len(recent)


def tag_emergency(c: dict[str, Any]) -> None:
    """Older calls ended as plain hang-ups even when the caller was sent to 911."""
    import re
    from .rules import emergency_in
    if c.get("status") != "ended" or c.get("decision"):
        return
    lines = c.get("transcript") or []
    danger = next((emergency_in(l["text"]) for l in lines if l["role"] == "caller" and emergency_in(l["text"])), None)
    sent = any(l["role"] == "agent" and re.search(r"\b(9-?1-?1|nine[- ]one[- ]one)\b", l["text"], re.I) for l in lines)
    if danger or sent:
        c["status"] = "emergency"
        why = f'caller said "{danger}"' if danger else "agent sent the caller to 911"
        c["alert"] = f"Emergency: {why}."


def guava_transcript(call_id: str) -> list[dict[str, Any]] | None:
    if not os.environ.get("GUAVA_API_KEY"):
        return None
    try:
        r = httpx.get(f"{API}/v1/conversations/{call_id}/transcript", headers=_headers(), timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        turns = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Guava transcript for %s failed: %s", call_id, exc)
        return None
    return [{"role": "caller" if t.get("speaker") == "HUMAN" else "agent", "uid": None, "text": t.get("text", ""),
             "t": (t.get("offset_ms") or 0) / 1000, "partial": False, "interrupted": False, "source": "guava"}
            for t in turns if t.get("text")]


def recording(call_id: str) -> httpx.Response:
    return httpx.get(f"{API}/v1/conversations/{call_id}/recording", headers=_headers(), timeout=60)


def fill_from_guava(call_id: str) -> bool:
    """Replace a missing transcript with Guava's. Returns True if it changed."""
    call = bus.get(call_id)
    if not call or call.get("simulated"):
        return False
    turns = guava_transcript(call_id)
    if not turns or len(turns) <= len(call.get("transcript") or []):
        return False
    with bus._lock:
        bus.calls[call_id]["transcript"] = turns
        bus.calls[call_id]["transcript_source"] = "guava"
        record = json.loads(json.dumps(bus.calls[call_id]))
    with CALL_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")
    bus.broadcast({"type": "transcript", "call_id": call_id, "transcript": turns})
    return True


def fetch_after_call(call_id: str, delay: float = 4.0) -> None:
    """Guava finalizes the transcript a moment after hangup."""
    def run() -> None:
        time.sleep(delay)
        fill_from_guava(call_id)
    threading.Thread(target=run, daemon=True, name=f"transcript-{call_id}").start()
