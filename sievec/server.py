"""Dashboard: static page, WebSocket event stream, review + settings API."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import history, llm
from .audit import audit
from .bus import bus
from .rules import LEARNED_DIR, rules
from .sim import SCENARIOS, run_scenario

STATIC = Path(__file__).resolve().parent.parent / "static"


def stats() -> dict:
    calls = bus.snapshot()["calls"]
    routed = Counter(
        c["decision"]["category"]["label"] for c in calls
        if c.get("decision") and c["decision"].get("cell")
    )
    return {
        "total": len(calls),
        "live": sum(1 for c in calls if c["status"] in ("listening", "routing", "transferring")),
        "options": sum(len(r["cells"]) for r in rules.public_table()["rows"]),
        "transferred": sum(1 for c in calls if c["status"] in ("transferred", "handoff")),
        "emergencies": sum(1 for c in calls if c["status"] == "emergency"),
        "by_service": [{"label": k, "count": v} for k, v in routed.most_common(6)],
        "feedback": rules.feedback_stats(),
        "cities_learned": sum(1 for k in rules.jurisdictions if k.startswith("city:")),
    }


def detail_stats() -> dict:
    """Routing outcomes across the whole call log (not just what's on screen)."""
    from .bus import CALL_LOG
    calls: dict[str, dict] = {}
    if CALL_LOG.exists():
        for line in CALL_LOG.read_text().splitlines():
            if line.strip():
                try:
                    c = json.loads(line)
                    calls[c["id"]] = c
                except (json.JSONDecodeError, KeyError):
                    pass
    for c in bus.snapshot()["calls"]:
        calls[c["id"]] = c
    routed = [c for c in calls.values() if (c.get("decision") or {}).get("cell")]
    by_dest: dict[tuple, dict] = {}
    for c in routed:
        d = c["decision"]
        cell, cat, place = d["cell"], d["category"], d.get("place") or {}
        k = (cell.get("department"), cell.get("phone_display"), cat["label"], place.get("key"))
        row = by_dest.setdefault(k, {"department": k[0], "phone": k[1], "service": k[2],
                                     "jurisdiction": rules.jurisdictions.get(k[3], {}).get("short") or place.get("name"),
                                     "count": 0, "live": 0, "sample": 0, "fallback": 0})
        row["count"] += 1
        row["sample" if c.get("simulated") else "live"] += 1
        row["fallback"] += 1 if d.get("fallback") else 0
    def tally(fn) -> list[dict]:
        out = Counter(fn(c) for c in routed)
        return [{"label": k, "count": v} for k, v in out.most_common() if k]
    times = []
    for c in routed:
        rule = next((s for s in c.get("pipeline") or [] if s["key"] == "rule" and s.get("t")), None)
        if rule:
            times.append(rule["t"] - c["started_at"])
    statuses = Counter(c.get("status") for c in calls.values())
    return {
        "calls": len(calls),
        "live_calls": sum(1 for c in calls.values() if not c.get("simulated")),
        "routed": len(routed),
        "handed_off": statuses.get("handoff", 0) + statuses.get("transferred", 0),
        "emergencies": statuses.get("emergency", 0),
        "outside_area": statuses.get("no-match", 0),
        "hung_up_early": sum(1 for c in calls.values() if c.get("status") == "ended" and not c.get("decision")),
        "median_seconds_to_route": round(sorted(times)[len(times) // 2], 1) if times else None,
        "destinations": sorted(by_dest.values(), key=lambda r: -r["count"]),
        "by_service": tally(lambda c: c["decision"]["category"]["label"]),
        "by_jurisdiction": tally(lambda c: (c["decision"].get("place") or {}).get("name", "").split(" (")[-1].rstrip(")")
                                 if "(" in (c["decision"].get("place") or {}).get("name", "")
                                 else (c["decision"].get("place") or {}).get("name")),
        "by_method": tally(lambda c: c["decision"]["category"].get("method", "").split(" (")[0]),
        "by_place_method": tally(lambda c: (c["decision"].get("place") or {}).get("method", "").split(" (")[0]),
    }


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.bind_loop(asyncio.get_running_loop())
        yield

    app = FastAPI(title="Sievec", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state():
        return {**bus.snapshot(), "stats": stats(), "rules": rules.public_table()}

    @app.get("/api/stats")
    async def stats_detail():
        return await asyncio.to_thread(detail_stats)

    @app.get("/api/scenarios")
    async def scenarios():
        return [{"id": k, "title": v["title"]} for k, v in SCENARIOS.items()]

    @app.post("/api/simulate/{name}")
    async def simulate(name: str):
        if name not in SCENARIOS:
            raise HTTPException(404, "Unknown scenario")
        return {"call_id": run_scenario(name)}

    @app.get("/api/settings")
    async def settings():
        return {**bus.config, "llm": llm.config(), "rules_file": "data/rules.json",
                "categories": len(rules.categories), "columns": len(rules.jurisdictions)}

    @app.post("/api/learned/reset")
    async def reset_learned():
        """Forget cities learned during calls (back to the seed set), so the
        learning demo can run again."""
        shutil.rmtree(LEARNED_DIR, ignore_errors=True)
        rules.reload()
        bus.broadcast({"type": "rules", "rules": rules.public_table()})
        bus.broadcast({"type": "stats", "stats": stats()})
        return {"ok": True}

    @app.post("/api/calls/{call_id}/transcript/refresh")
    async def refresh_transcript(call_id: str):
        """Pull Guava's own transcript for a past call whose live capture is empty."""
        changed = await asyncio.to_thread(history.fill_from_guava, call_id)
        call = bus.get(call_id)
        return {"changed": changed, "lines": len((call or {}).get("transcript") or [])}

    @app.get("/api/calls/{call_id}/recording")
    async def call_recording(call_id: str):
        call = bus.get(call_id)
        if not call or call.get("simulated"):
            raise HTTPException(404, "No recording for this call")
        r = await asyncio.to_thread(history.recording, call_id)
        if r.status_code != 200:
            raise HTTPException(r.status_code, "Guava has no recording for this call yet")
        return Response(r.content, media_type="audio/wav")

    @app.get("/api/calls/{call_id}/audit")
    async def call_audit(call_id: str):
        return {"call_id": call_id, "records": audit.for_call(call_id), "chain": audit.verify()}

    @app.get("/api/audit/verify")
    async def audit_verify():
        return audit.verify()

    class Feedback(BaseModel):
        correct: str

    @app.post("/api/calls/{call_id}/feedback")
    async def feedback(call_id: str, body: Feedback):
        call = bus.get(call_id)
        if not call or not call.get("decision"):
            raise HTTPException(404, "That call has no routing decision to review")
        if body.correct not in rules.categories:
            raise HTTPException(400, "Unknown service")
        predicted = call["decision"]["category"]["id"]
        caller_lines = [l["text"] for l in call["transcript"] if l["role"] == "caller"]
        result = rules.record_feedback({
            "call_id": call_id,
            "phrase": caller_lines[0] if caller_lines and body.correct != predicted else "",
            "location": call["fields"].get("location", ""),
            "predicted": predicted,
            "correct": body.correct,
        })
        bus.note(call_id, "operator.review", {"predicted": predicted, "correct": body.correct,
                                               "learned": result["learned"]})
        bus.status(call_id, call["status"], feedback={
            "correct": body.correct, "matched": body.correct == predicted, "learned": result["learned"]})
        bus.broadcast({"type": "stats", "stats": stats()})
        return {"ok": True, **result}

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        q = bus.subscribe()
        try:
            await socket.send_text(json.dumps({"type": "hello", **bus.snapshot(), "stats": stats(),
                                               "rules": rules.public_table()}))
            while True:
                msg = await q.get()
                await socket.send_text(msg)
                if msg.startswith('{"type": "call"'):
                    await socket.send_text(json.dumps({"type": "stats", "stats": stats()}))
        except WebSocketDisconnect:
            pass
        finally:
            bus.unsubscribe(q)

    return app
