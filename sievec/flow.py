"""The routing pipeline, shared by live calls and simulated ones.

    area code guess -> place confirmed -> service category -> table rule -> connect

Each step reports to the dashboard as it happens. `say` and `finish` are how
the pipeline talks back: on a live call they become Guava commands, in the
simulator they become transcript lines.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import learn, places
from .bus import bus
from .llm import current_call
from .rules import rules

logger = logging.getLogger("sievec.flow")

# handoff (default): tell the caller we're forwarding them, then produce the
#          briefing the outbound leg would speak to the department (no outbound
#          calling on this account, so the briefing is the end of the demo).
# demo:    really transfer, but to SIEVEC_DEMO_NUMBER (your phone).
# live:    really transfer to the department.
TRANSFER_MODE = os.environ.get("SIEVEC_TRANSFER", "handoff").lower()
DEMO_NUMBER = os.environ.get("SIEVEC_DEMO_NUMBER", "").strip()


@dataclass
class Outcome:
    action: str                 # "transfer" | "hangup"
    target: str | None = None
    instructions: str = ""


def learn_progress(event: dict[str, Any]) -> None:
    bus.broadcast({"type": "learn", **event})
    if event["state"] == "done":
        bus.broadcast({"type": "rules", "rules": rules.public_table()})


def transfer_target(cell: dict[str, Any]) -> str | None:
    if TRANSFER_MODE == "live":
        return cell.get("phone")
    if TRANSFER_MODE == "demo":
        return DEMO_NUMBER or None
    return None


def briefing(call_id: str, decision: dict[str, Any], location: str) -> dict[str, Any]:
    """What the outbound leg says to the department before bridging the caller.

    A fixed template filled from the decision, not generated text, so it's
    predictable and auditable."""
    snap = bus.get(call_id) or {}
    cell, cat, place = decision["cell"], decision["category"], decision["place"]
    caller_lines = [l["text"] for l in snap.get("transcript", []) if l["role"] == "caller"]
    words = max(caller_lines, key=len) if caller_lines else ""
    callback = snap.get("caller")
    ref = call_id[-6:].upper()
    where = location if location and location.lower() not in ("yes", "yeah") else place["name"]
    script = (
        f"Hi, this is Rosa, an automated assistant with Sievec, the Los Angeles city and county services line. "
        f"I have a resident on the line who needs {cat['label'].lower()}, at {where}, {place['name']}. "
        + (f"In their words: \"{words}\". " if words else "")
        + (f"Their callback number is {_spoken_phone(callback)}. " if callback else "")
        + f"Our reference is {' '.join(ref)}. I'm connecting them to you now."
    )
    return {
        "to": {"department": cell["department"], "service": cell["service"], "phone": cell.get("phone"),
               "phone_display": cell.get("phone_display"), "source_url": cell.get("source_url")},
        "facts": {"service": cat["label"], "location": where, "jurisdiction": place["name"],
                  "callback": callback, "reference": ref, "rule": decision["rule"]},
        "script": script,
    }


def _spoken_phone(n: str | None) -> str:
    d = "".join(ch for ch in (n or "") if ch.isdigit())[-10:]
    return f"{d[:3]} {d[3:6]} {d[6:]}" if len(d) == 10 else (n or "")


# -- System 1 while the caller is talking ----------------------------------------

_s1_timers: dict[str, threading.Timer] = {}


def schedule_system1(call_id: str, caller_text: str, delay: float = 0.8) -> None:
    """Debounced fast-model read of what the caller has said so far."""
    if t := _s1_timers.pop(call_id, None):
        t.cancel()

    def run() -> None:
        token = current_call.set(call_id)
        started = time.time()
        try:
            cid = rules._llm_category(caller_text, rules.rank(caller_text))
            bus.system1(call_id, {"id": cid, "label": rules.categories[cid]["label"],
                                  "latency_ms": int((time.time() - started) * 1000)})
        finally:
            current_call.reset(token)

    t = threading.Timer(delay, run)
    t.daemon = True
    _s1_timers[call_id] = t
    t.start()


# -- the decision ----------------------------------------------------------------

def decide(call_id: str, service_value: str, location: str, transcript: str, guess: dict[str, Any],
           say: Callable[[str], None]) -> Outcome:
    token = current_call.set(call_id)
    try:
        return _decide(call_id, service_value, location, transcript, guess, say)
    finally:
        current_call.reset(token)


def _decide(call_id, service_value, location, transcript, guess, say) -> Outcome:
    snap = bus.get(call_id) or {}
    caller_text = " ".join(l["text"] for l in snap.get("transcript", []) if l["role"] == "caller")
    bus.status(call_id, "routing", fields={"service": service_value, "location": location})

    # 1. Place
    bus.step(call_id, "place", "active", location or "confirming area code guess")

    def place_progress(ev: dict[str, Any]) -> None:
        if ev["state"] == "searching":
            say("Tell the caller you're checking which city handles that address. It'll take a few seconds.")
            bus.step(call_id, "place", "active", location, "Not in the cache. Searching the web.")
        elif ev["state"] == "reading":
            bus.step(call_id, "place", "active", location, f"Reading {len(ev['sources'])} sources with System 2")

    place = places.resolve(location, guess, on_progress=place_progress)
    bus.step(call_id, "place", "done", place["name"], place["method"], data=place)

    # 2. Service category
    bus.step(call_id, "service", "active", "classifying")
    cat = rules.pick_category(f"{service_value}. {caller_text}", hint_label=service_value)
    bus.lanes(call_id, cat["lanes"])
    category = rules.categories[cat["id"]]
    bus.step(call_id, "service", "done", category["label"], cat["method"], data={"id": cat["id"]})

    decision: dict[str, Any] = {"category": {"id": cat["id"], "label": category["label"], "method": cat["method"]},
                                "place": place, "cell": None, "rule": None}

    # Emergencies never transfer.
    if category.get("emergency"):
        decision["rule"] = f"{category['label']} → 911"
        bus.step(call_id, "rule", "done", "Emergency", "Never transferred")
        bus.step(call_id, "connect", "done", "Dial 911")
        bus.status(call_id, "emergency", decision=decision, alert="Emergency. Caller told to hang up and dial 911.")
        extra = (" Tell them to get out of the building first, then call nine-one-one or SoCalGas at 1-800-427-2200."
                 if cat["id"] == "gas_leak" else "")
        return Outcome("hangup", instructions=f"Tell the caller to hang up and dial nine-one-one right now.{extra}")

    if place["kind"] == "outside" or not place.get("key"):
        bus.step(call_id, "rule", "done", "Outside LA County")
        bus.step(call_id, "connect", "done", "No transfer")
        bus.status(call_id, "no-match", decision=decision)
        return Outcome("hangup", instructions=(
            f"Explain that this line covers Los Angeles County, and {place['name']} is outside it. "
            "Suggest calling their own city hall, then say goodbye."))

    # 3. Rule lookup. A missing cell is filled by searching
    #    "<place> <service> phone number" and stored in the rules.
    key = place["key"]
    cell = rules.lookup(cat["id"], key)
    if cell is None and place["kind"] == "other_city":
        city = place["name"].replace("City of ", "")
        query = learn.search_query(city, cat["id"])
        bus.step(call_id, "rule", "active", f"{category['label']} × {place['name']}",
                 f'Not in the rules yet. Searching "{query}"')
        say(f"Tell the caller you're looking up the right {city} office for that. Just a moment.")
        rules.ensure_jurisdiction(key, place["name"])
        bus.broadcast({"type": "rules", "rules": rules.public_table()})
        cell = learn.learn_service(key, city, cat["id"], learn_progress)
        bus.broadcast({"type": "rules", "rules": rules.public_table()})
        # Fill the rest of this city's column in the background (for the board).
        if not (rules.jurisdictions.get(key) or {}).get("complete"):
            learn.learn_in_background(key, city, learn_progress, call_id)
        if cell is None and rules.lookup("general", key) is None:
            bus.step(call_id, "rule", "active", f"{category['label']} × {place['name']}",
                     f'Nothing found. Searching "{learn.search_query(city, "general")}"')
            learn.learn_service(key, city, "general", learn_progress)
    fallback = False
    if cell is None and place["kind"] == "other_city":
        cell, fallback = rules.lookup("general", key), True
    if cell is None:
        cell, fallback = rules.lookup("general", "la_city" if place["kind"] == "la_city" else "la_county"), True
    if fallback:
        cell = {**cell, "notes": f"No direct {category['label'].lower()} line found; this is the general line. "
                                 + (cell.get("notes") or "")}

    decision["cell"] = cell
    decision["fallback"] = fallback
    decision["rule"] = f"{category['label']} × {rules.jurisdictions.get(key, {}).get('short', place['name'])}"
    bus.step(call_id, "rule", "done", cell["department"],
             decision["rule"] + (" (general line, no direct number found)" if fallback else ""),
             data={"category": cat["id"], "column": key, "fallback": fallback})

    # 4. Connect
    brief = briefing(call_id, decision, location)
    decision["handoff"] = brief
    bus.note(call_id, "handoff.briefing", brief)
    target = transfer_target(cell)
    transfer = {"mode": TRANSFER_MODE, "to": target, "department_phone": cell.get("phone")}
    bus.step(call_id, "connect", "active", cell.get("phone_display"),
             {"live": "Transferring", "demo": "Transferring to the demo phone",
              "handoff": "Briefing the department"}.get(TRANSFER_MODE, "Reading the number"))
    bus.status(call_id, "transferring", decision=decision, transfer=transfer)

    forward = (f"Say: \"Ok great, I'm going to forward you to {cell['department']}. I'll tell them what's going on "
               f"and transfer the call.\" Also give them the direct number, {cell['phone_display']}, in case the "
               f"call drops.")
    if target:
        return Outcome("transfer", target, forward + " Then transfer.")
    return Outcome("hangup", instructions=forward + " Then end the call politely.")


def answer_question(call_id: str, question: str) -> str:
    """on_question: facts from the decided cell, or a quick lookup."""
    decision = (bus.get(call_id) or {}).get("decision") or {}
    cell = decision.get("cell")
    if cell:
        return (f"{cell['department']} ({cell['service']}), phone {cell.get('phone_display')}. "
                f"Hours: {cell.get('hours') or 'not listed'}. {cell.get('notes') or ''}")
    lanes = rules.rank(question)
    if lanes:
        cell = rules.lookup(lanes[0]["id"], "la_city")
        if cell:
            return f"In the City of Los Angeles that's {cell['department']}, {cell['phone_display']}."
    return "I don't have that detail, but I can connect you with the office that does."

