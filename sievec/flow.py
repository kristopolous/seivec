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


def schedule_system1(call_id: str, caller_text: str, delay: float = 0.7,
                     on_result: Callable[[dict[str, Any]], None] | None = None, latest: str | None = None) -> None:
    """Debounced System One read of everything the caller has said so far:
    service probabilities (they drive the lanes) plus emergency / wants a
    person / urgency / pace, which `on_result` turns into actions."""
    if t := _s1_timers.pop(call_id, None):
        t.cancel()

    def run() -> None:
        token = current_call.set(call_id)
        try:
            r = rules.ask_systemone(caller_text, latest)
        except Exception as exc:  # noqa: BLE001
            logger.warning("System One read failed: %s", exc)
            return
        finally:
            current_call.reset(token)
        snap = bus.get(call_id) or {}
        if snap.get("status") in ("listening",) and r["lanes"]:
            bus.lanes(call_id, r["lanes"])
        bus.system1(call_id, r)
        if on_result:
            on_result(r)

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
            bus.annotate(call_id, f"\"{location}\" isn't in the place cache: searching the web for its jurisdiction.", "search")
        elif ev["state"] == "reading":
            bus.step(call_id, "place", "active", location, f"Reading {len(ev['sources'])} sources with System 2")

    place = places.resolve(location, guess, on_progress=place_progress)
    if place.get("method") == "web search":
        bus.annotate(call_id, f"Web search placed it in {place['name']}.", "search")
    bus.step(call_id, "place", "done", place["name"], place["method"], data=place)

    # 2. Service category
    bus.step(call_id, "service", "active", "classifying")
    cat = rules.pick_category(f"{service_value}. {caller_text}", hint_label=service_value)
    bus.lanes(call_id, cat["lanes"])
    discovered_cell = None
    decision_answer = None
    if cat["id"] == "general" and place.get("key"):
        # Not in the table: work out a search, and if it turns up a phone
        # number, use it and keep it as a new service row.
        bus.step(call_id, "service", "active", "Not in the rules", "Working out what to search for")
        say("Tell the caller you're looking up the right office for that. Just a moment.")

        def discover_progress(ev: dict[str, Any]) -> None:
            learn_progress(ev)
            if ev["state"] == "searching":
                bus.step(call_id, "service", "active", ev["label"], f'Searching "{ev["query"]}"')

        # Pass what they said as the question too, so "is it open today?" gets answered.
        found = learn.discover(f"{service_value}. {caller_text}", place["key"], place["name"], discover_progress,
                               question=caller_text)
        bus.broadcast({"type": "rules", "rules": rules.public_table()})
        if found and found.get("answer"):
            decision_answer = found["answer"]
        if found and found.get("cell"):
            bus.annotate(call_id, f"Searched \"{found['query']}\" → {found['cell']['department']}, "
                                  f"{found['cell']['phone_display']}. Added \"{found['label']}\" to the rules.", "learned")
            cat = {"id": found["category"], "label": found["label"], "lanes": cat["lanes"],
                   "method": f'web search "{found["query"]}", added to the rules'}
            discovered_cell = found["cell"]
        elif found and found.get("existing"):
            cat = {**cat, "id": found["category"], "method": "System 2 matched an existing service"}
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

    if not place.get("key"):
        # We couldn't pin the place down at all: ask for it rather than refuse.
        bus.step(call_id, "rule", "done", "Need a city")
        bus.step(call_id, "connect", "done", "Asked again")
        bus.status(call_id, "no-match", decision=decision)
        return Outcome("hangup", instructions=(
            "Apologize that you couldn't place that location, give them the number for 2-1-1 for general help, "
            "and invite them to call back with the city name."))

    # 3. Rule lookup. A missing cell is filled by searching
    #    "<place> <service> phone number" and stored in the rules.
    key = place["key"]
    cell = discovered_cell or rules.lookup(cat["id"], key)
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
        bus.annotate(call_id, (f"Searched \"{query}\" → {cell['department']}, {cell['phone_display']}. Saved to the rules."
                               if cell else f"Searched \"{query}\": no clear number found."), "learned" if cell else "search")
        # Fill the rest of this city's column in the background (for the board).
        if not (rules.jurisdictions.get(key) or {}).get("complete"):
            learn.learn_in_background(key, city, learn_progress, call_id)
        if cell is None and rules.lookup("general", key) is None:
            bus.step(call_id, "rule", "active", f"{category['label']} × {place['name']}",
                     f'Nothing found. Searching "{learn.search_query(city, "general")}"')
            gen = learn.learn_service(key, city, "general", learn_progress)
            gq = learn.search_query(city, "general")
            bus.annotate(call_id, (f"Searched \"{gq}\" → {gen['department']}, {gen['phone_display']} "
                                   f"(the city's general line). Saved to the rules." if gen
                                   else f"Searched \"{gq}\": nothing found."), "learned" if gen else "search")
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

    answer = (f"First, answer their question in one short sentence: {decision_answer} Then say: "
              if decision_answer else "Say: ")
    forward = (f"{answer}\"Ok great, I'm going to forward you to {cell['department']}. I'll tell them what's going on "
               f"and transfer the call.\" Also give them the direct number, {cell['phone_display']}, in case the "
               f"call drops.")
    if target:
        return Outcome("transfer", target, forward + " Then transfer.")
    return Outcome("hangup", instructions=forward + " Then end the call politely.")


def answer_question(call_id: str, question: str) -> str:
    """on_question: same move as routing. Work out a search, read the pages,
    answer from them, and keep the office + number as a rule."""
    token = current_call.set(call_id)
    try:
        snap = bus.get(call_id) or {}
        decision = snap.get("decision") or {}
        said = " ".join(l["text"] for l in snap.get("transcript", []) if l["role"] == "caller")[-600:]
        place = decision.get("place") or places.known_place(said) or {"key": "la_city", "name": "Los Angeles"}
        bus.note(call_id, "question.asked", {"question": question})
        found = learn.discover(said or question, place.get("key"), place.get("name", ""), learn_progress,
                               question=question)
        bus.broadcast({"type": "rules", "rules": rules.public_table()})
        cell = (found or {}).get("cell")
        answer = ((found or {}).get("answer") or "").strip()
        if cell:
            reply = (f"{answer} " if answer else "") + (
                f"That's {cell['department']}, {cell['phone_display']}. I can connect you if you'd like.")
        else:
            reply = answer or "I couldn't find that, but I can connect you with the office that handles it."
        bus.annotate(call_id, f"Question looked up on the web"
                              + (f" (\"{found['query']}\")" if found and found.get("query") else "")
                              + f": {reply}", "answer")
        bus.note(call_id, "question.answered", {"question": question, "answer": reply,
                                                "query": (found or {}).get("query"),
                                                "source_url": (cell or {}).get("source_url")})
        return reply
    finally:
        current_call.reset(token)
