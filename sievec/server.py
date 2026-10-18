"""Dashboard: static page, WebSocket event stream, review + settings API."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import llm
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
