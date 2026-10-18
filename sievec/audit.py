"""Append-only, hash-chained audit trail of every routing decision.

Each record carries the SHA-256 of the record before it, so editing or
deleting any line breaks the chain from that point on (`verify()` finds it).
Model and web payloads are stored as digests plus a short summary; the full
payloads live in the trace log.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

PATH = Path(__file__).resolve().parent.parent / "data" / "audit.jsonl"
GENESIS = "0" * 64


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Audit:
    def __init__(self, path: Path = PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._seq = 0
        self._prev = GENESIS
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._seq, self._prev = rec["seq"], rec["hash"]

    def record(self, call_id: str | None, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            body = {"seq": self._seq, "ts": round(time.time(), 3), "call_id": call_id, "event": event,
                    "payload": payload, "prev": self._prev}
            body["hash"] = _digest(body)
            self._prev = body["hash"]
            self.path.parent.mkdir(exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(body) + "\n")
            return body

    def for_call(self, call_id: str) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                if rec["call_id"] == call_id:
                    out.append(rec)
        return out

    def verify(self) -> dict[str, Any]:
        prev, n = GENESIS, 0
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                claimed = rec.pop("hash")
                if rec["prev"] != prev or _digest(rec) != claimed:
                    return {"ok": False, "records": n, "broken_at": rec["seq"]}
                prev, n = claimed, n + 1
        return {"ok": True, "records": n, "head": prev}


def summarize_trace(t: dict[str, Any]) -> dict[str, Any]:
    return {"kind": t["kind"], "model": t.get("model"), "system": t.get("system"), "purpose": t["purpose"],
            "latency_ms": t["latency_ms"], "error": bool(t.get("error")),
            "request_sha256": _digest(t["request"]), "response_sha256": _digest(t["response"]), "trace_id": t["id"]}


audit = Audit()
