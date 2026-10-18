"""In-memory call state plus a fan-out to every connected dashboard.

Guava handlers run on SDK worker threads; the dashboard lives on the uvicorn
event loop. Everything that mutates state goes through `Bus`, which takes a
lock, updates the snapshot, and hands the event to the loop thread-safely.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

from .audit import audit, summarize_trace

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CALL_LOG = DATA_DIR / "calls.jsonl"
MAX_CALLS = 50
MAX_TRACES = 300
STEPS = [("area", "Area code"), ("place", "Location"), ("service", "Service"), ("rule", "Rule"), ("connect", "Connect")]


class Bus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set[asyncio.Queue] = set()
        self.calls: dict[str, dict[str, Any]] = {}
        self.config: dict[str, Any] = {}
        self.traces: list[dict[str, Any]] = []

    # -- wiring -------------------------------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            calls = sorted(self.calls.values(), key=lambda c: c["started_at"], reverse=True)
            return {"config": self.config, "calls": json.loads(json.dumps(calls)),
                    "traces": json.loads(json.dumps(self.traces[-120:]))}

    def _emit(self, event: dict[str, Any]) -> None:
        if self._loop is None:
            return
        payload = json.dumps(event)

        def fan_out() -> None:
            for q in list(self._clients):
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

        self._loop.call_soon_threadsafe(fan_out)

    def _audit(self, call_id: str | None, event: str, payload: dict[str, Any]) -> None:
        rec = audit.record(call_id, event, payload)
        self._emit({"type": "audit", "record": rec})

    def note(self, call_id: str, event: str, payload: dict[str, Any]) -> None:
        """Audit an event that isn't a state change (handoff briefing, feedback, ...)."""
        self._audit(call_id, event, payload)

    def annotate(self, call_id: str, text: str, kind: str = "info") -> None:
        """A note in the transcript, at the moment Sievec did something."""
        with self._lock:
            call = self.calls.get(call_id)
            if call is None:
                return
            call["transcript"].append({"role": "system", "uid": None, "text": text, "t": time.time(),
                                       "partial": False, "interrupted": False, "kind": kind})
            transcript = json.loads(json.dumps(call["transcript"]))
        self._emit({"type": "transcript", "call_id": call_id, "transcript": transcript})

    def _update(self, call_id: str, kind: str, **changes: Any) -> None:
        with self._lock:
            call = self.calls.get(call_id)
            if call is None:
                return
            call.update(changes)
            snapshot = json.loads(json.dumps(call))
        self._emit({"type": kind, "call": snapshot})

    # -- call lifecycle -----------------------------------------------------

    def call_started(self, call_id: str, channel: str, caller: str | None, simulated: bool = False,
                     guess: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.calls[call_id] = {
                "id": call_id,
                "channel": channel,
                "caller": caller,
                "simulated": simulated,
                "started_at": time.time(),
                "status": "listening",
                "transcript": [],
                "lanes": [],
                "system1": None,
                "guess": guess or {},
                "pipeline": [{"key": k, "label": l, "state": "pending", "value": None, "detail": None}
                             for k, l in STEPS],
                "fields": {},
                "decision": None,
                "transfer": None,
                "ended_reason": None,
                "ended_at": None,
                "alert": None,
                "feedback": None,
            }
            if len(self.calls) > MAX_CALLS:
                oldest = min(self.calls.values(), key=lambda c: c["started_at"])
                self.calls.pop(oldest["id"], None)
        self._audit(call_id, "call.started", {"channel": channel, "caller": caller, "simulated": simulated,
                                              "area_code_guess": guess or {}})
        self._update(call_id, "call")

    def speech(self, call_id: str, role: str, text: str, utterance_id: str | None = None,
               interrupted: bool = False) -> None:
        """Append or revise a transcript line. Caller partials share an utterance_id."""
        with self._lock:
            call = self.calls.get(call_id)
            if call is None:
                return
            lines = call["transcript"]
            for line in lines:
                if utterance_id and line.get("uid") == utterance_id and line["role"] == role:
                    line["text"] = text
                    break
            else:
                for line in lines:
                    if line["role"] == "caller":
                        line["partial"] = False
                lines.append({
                    "role": role,
                    "uid": utterance_id,
                    "text": text,
                    "t": time.time(),
                    "partial": role == "caller",
                    "interrupted": interrupted,
                })
            if role == "agent":
                for line in lines:
                    if line["role"] == "caller":
                        line["partial"] = False
            transcript = json.loads(json.dumps(lines))
        self._emit({"type": "transcript", "call_id": call_id, "transcript": transcript})

    def lanes(self, call_id: str, lanes: list[dict[str, Any]]) -> None:
        self._update(call_id, "lanes", lanes=lanes)

    def step(self, call_id: str, key: str, state: str, value: Any = None, detail: Any = None,
             data: Any = None) -> None:
        with self._lock:
            call = self.calls.get(call_id)
            if call is None:
                return
            for st in call["pipeline"]:
                if st["key"] == key:
                    st.update(state=state, value=value, detail=detail, data=data, t=time.time())
            pipeline = json.loads(json.dumps(call["pipeline"]))
        if state == "done":
            self._audit(call_id, f"step.{key}", {"value": value, "detail": detail, "data": data})
        self._emit({"type": "pipeline", "call_id": call_id, "pipeline": pipeline})

    def system1(self, call_id: str, result: dict[str, Any]) -> None:
        self._update(call_id, "system1", system1=result)

    def trace(self, rec: dict[str, Any]) -> None:
        with self._lock:
            self.traces.append(rec)
            del self.traces[:-MAX_TRACES]
        if rec.get("purpose") != "warm-up":
            self._audit(rec.get("call_id"), f"{rec['kind']}.request", summarize_trace(rec))
        self._emit({"type": "trace", "trace": rec})

    def status(self, call_id: str, status: str, **extra: Any) -> None:
        payload = {"status": status}
        if "decision" in extra:
            d = extra["decision"] or {}
            cell = d.get("cell") or {}
            payload["decision"] = {"rule": d.get("rule"), "category": d.get("category"),
                                   "place": {k: (d.get("place") or {}).get(k) for k in ("key", "name", "method", "sources")},
                                   "department": cell.get("department"), "phone": cell.get("phone"),
                                   "source_url": cell.get("source_url")}
        if "transfer" in extra:
            payload["transfer"] = extra["transfer"]
        self._audit(call_id, "call.status", payload)
        self._update(call_id, "call", status=status, **extra)

    def alert(self, call_id: str, message: str) -> None:
        self._audit(call_id, "call.alert", {"message": message})
        self._update(call_id, "call", alert=message)

    def ended(self, call_id: str, reason: str) -> None:
        with self._lock:
            call = self.calls.get(call_id)
            if call is None:
                return
            if call["status"] not in ("transferred", "emergency", "handoff"):
                call["status"] = "ended"
            call["ended_reason"] = reason
            call["ended_at"] = time.time()
            for line in call["transcript"]:
                line["partial"] = False
            record = json.loads(json.dumps(call))
        self._audit(call_id, "call.ended", {"reason": reason, "status": record["status"],
                                            "duration_s": round(record["ended_at"] - record["started_at"], 1)})
        DATA_DIR.mkdir(exist_ok=True)
        with CALL_LOG.open("a") as f:
            f.write(json.dumps(record) + "\n")
        self._emit({"type": "call", "call": record})

    def get(self, call_id: str) -> dict[str, Any] | None:
        with self._lock:
            call = self.calls.get(call_id)
            return json.loads(json.dumps(call)) if call else None

    def broadcast(self, event: dict[str, Any]) -> None:
        self._emit(event)


bus = Bus()
