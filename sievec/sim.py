"""Scripted calls for rehearsing the demo without a phone line.

They run the real pipeline (keyword lanes, System 1 model, place resolution,
web learning, table lookup), so the only thing simulated is the audio.
"""

from __future__ import annotations

import random
import threading
import time
import uuid

from . import flow, places
from .bus import bus
from .rules import rules

GREETING = "Sievec, Los Angeles city and county services. This is Rosa. What can I help you with?"

SCENARIOS = {
    "possum": {
        "title": "Dead possum, area code confirmed",
        "caller": "+13235550142",
        "service": "Dead animal pickup",
        "location": "yes",
        "script": [
            ("agent", GREETING),
            ("caller", "Hi, um, there's a dead possum on the sidewalk in front of my building"),
            ("agent", "Sorry you're dealing with that. Is this in the City of Los Angeles?"),
            ("caller", "Yes, it's Sunset and Vermont"),
        ],
    },
    "couch": {
        "title": "Couch on the curb, Van Nuys",
        "caller": "+18185550199",
        "service": "",
        "location": "14500 Sherman Way, Van Nuys",
        "script": [
            ("agent", GREETING),
            ("caller", "I need someone to come get an old couch and a mattress, they're too big for the bin"),
            ("agent", "Got it. Is this in the City of Los Angeles?"),
            ("caller", "Yeah, 14500 Sherman Way in Van Nuys"),
        ],
    },
    "pothole_county": {
        "title": "Pothole, caller corrects to East LA",
        "caller": "+12135550163",
        "service": "",
        "location": "Whittier Blvd and Atlantic, East Los Angeles",
        "script": [
            ("agent", GREETING),
            ("caller", "There's a big hole in the road, like a pothole, in the right lane"),
            ("agent", "Thanks. Is this in the City of Los Angeles?"),
            ("caller", "No, it's East LA, Whittier Boulevard and Atlantic"),
        ],
    },
    "glendale": {
        "title": "Streetlight, Glendale (learns a new city)",
        "caller": "+18185550123",
        "service": "",
        "location": "Brand Blvd and Broadway, Glendale",
        "script": [
            ("agent", GREETING),
            ("caller", "The streetlight on my corner has been out for a week, it's really dark at night"),
            ("agent", "I can help with that. Is this in the City of Los Angeles?"),
            ("caller", "No, I'm in Glendale, Brand and Broadway"),
        ],
    },
    "stoplight": {
        "title": "Signal out downtown",
        "caller": "+12135550110",
        "service": "",
        "location": "Figueroa and 7th, downtown LA",
        "script": [
            ("agent", GREETING),
            ("caller", "The traffic light at Figueroa and Seventh is totally dark, cars are just going"),
            ("agent", "Thanks for calling that in. Is this in the City of Los Angeles?"),
            ("caller", "Yes, downtown"),
        ],
    },
    "gas": {
        "title": "Gas smell (emergency)",
        "caller": "+12135550101",
        "service": "",
        "location": "Pico and Western",
        "script": [
            ("agent", GREETING),
            ("caller", "I smell gas really strong outside my neighbor's house"),
        ],
    },
}


def _caller_text(call_id: str) -> str:
    snap = bus.get(call_id) or {}
    return " ".join(l["text"] for l in snap.get("transcript", []) if l["role"] == "caller")


def _speak(call_id: str, role: str, text: str) -> None:
    if role == "agent":
        bus.speech(call_id, "agent", text)
        time.sleep(0.8 + len(text) / 60)
        return
    uid = uuid.uuid4().hex[:10]
    words = text.split()
    for i in range(1, len(words) + 1):
        bus.speech(call_id, "caller", " ".join(words[:i]), uid)
        bus.lanes(call_id, rules.rank(_caller_text(call_id)))
        time.sleep(random.uniform(0.11, 0.24))
    flow.schedule_system1(call_id, _caller_text(call_id), delay=0.2, latest=text)
    time.sleep(0.9)


def run_scenario(name: str) -> str:
    sc = SCENARIOS[name]
    call_id = "sim-" + uuid.uuid4().hex[:8]

    def go() -> None:
        guess = places.guess_from_number(sc["caller"])
        bus.call_started(call_id, "phone", sc["caller"], simulated=True, guess=guess)
        if guess["key"]:
            bus.step(call_id, "area", "done", guess["area_code"],
                     f"{guess['region']}: guessing {guess['name']}", data=guess)
        time.sleep(0.6)
        for role, text in sc["script"]:
            _speak(call_id, role, text)
        transcript = "\n".join(f"{r}: {t}" for r, t in sc["script"])

        def say(instruction: str) -> None:
            if "checking which city" in instruction:
                bus.speech(call_id, "agent", "Let me check which city handles that address. One moment.")
            elif "looking up the right" in instruction:
                bus.speech(call_id, "agent", "I'm looking up the right office for you. Just a moment.")

        outcome = flow.decide(call_id, sc["service"], sc["location"], transcript, guess, say)
        snap = bus.get(call_id) or {}
        cell = (snap.get("decision") or {}).get("cell")
        if cell:
            bus.speech(call_id, "agent",
                       f"Ok great, I'm going to forward you to {cell['department']}. I'll tell them what's going on "
                       f"and transfer the call. Their direct number is {cell.get('phone_display')}, in case we get "
                       f"disconnected.")
            time.sleep(3.0)
            if outcome.action == "transfer":
                bus.step(call_id, "connect", "done", cell.get("phone_display"), "Connected")
                bus.status(call_id, "transferred")
                bus.ended(call_id, "bot-transfer")
            else:
                bus.step(call_id, "connect", "done", cell.get("phone_display"), "Handoff briefing ready")
                bus.status(call_id, "handoff")
                bus.ended(call_id, "bot-hangup")
        else:
            bus.speech(call_id, "agent", "If anyone is in danger, please hang up and dial 911 right now."
                       if snap.get("status") == "emergency" else "That's outside the area this line covers.")
            time.sleep(1.5)
            bus.ended(call_id, "bot-hangup")

    threading.Thread(target=go, daemon=True, name=f"sim-{name}").start()
    return call_id
