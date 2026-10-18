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
    "slow_down": {
        "title": "Caller asks Rosa to slow down (pothole)",
        "caller": "+13235550188",
        "service": "",
        "location": "Figueroa and Avenue 50, Highland Park",
        "script": [
            ("agent", GREETING),
            ("caller", "I'm sorry dear, you're talking way too fast for me, can you slow down?"),
            ("agent", "Of course. I'll slow down. What can I help you with?"),
            ("caller", "There's a big pothole on my street, my car almost lost a tire"),
            ("agent", "I'm sorry about that. Is this in the City of Los Angeles?"),
            ("caller", "Yes, Highland Park, Figueroa and Avenue Fifty"),
        ],
    },
    "hurry_up": {
        "title": "Urgent caller says hurry up (water main)",
        "caller": "+18185550147",
        "service": "",
        "location": "Ventura Blvd and Van Nuys Blvd, Sherman Oaks",
        "script": [
            ("agent", GREETING),
            ("caller", "Water is gushing out of the street right now, it's flooding the intersection, please hurry up"),
            ("agent", "Got it. Where is it?"),
            ("caller", "Ventura and Van Nuys, Sherman Oaks"),
        ],
    },
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


# More sample calls, worded the way people talk (not the service names), so
# System One has to work out the service. (id, title, caller number,
# what they say, their answer to "where?", location string)
_MORE = [
    ("la_graffiti", "Graffiti on a fence, Echo Park", "+12135550131",
     "Somebody spray painted gang tags all over the fence by the school", "Echo Park, Sunset and Alvarado", "Sunset and Alvarado, Echo Park"),
    ("la_abandoned", "Car abandoned for weeks, Van Nuys", "+18185550162",
     "There's a car with flat tires that hasn't moved in like a month on my block", "Van Nuys, on Sylmar Avenue", "Sylmar Avenue, Van Nuys"),
    ("la_noise", "Loud party at 2am, Koreatown", "+12135550177",
     "My neighbors are blasting music and it's two in the morning", "Koreatown, Wilshire and Normandie", "Wilshire and Normandie, Koreatown"),
    ("la_power", "Power out on the block, Silver Lake", "+13235550109",
     "The power just went out on our whole block", "Silver Lake, near the reservoir", "Silver Lake"),
    ("la_landlord", "Landlord won't fix the heater, Westlake", "+12135550184",
     "My landlord won't fix the heater and it's been three weeks", "Westlake, on Alvarado", "Alvarado Street, Westlake"),
    ("la_rats", "Rats at a restaurant, Downtown", "+12135550193",
     "I just saw rats running around the kitchen of a taco place", "Downtown LA, on Broadway", "Broadway, Downtown LA"),
    ("la_mosquitoes", "Green pool breeding mosquitoes, Reseda", "+18185550118",
     "The house next door has a green pool and we're getting eaten alive by mosquitoes", "Reseda, off Victory", "Victory Blvd, Reseda"),
    ("la_freeway", "Debris on the 101", "+13235550127",
     "There's a mattress in the middle lane of the 101 freeway", "The 101 near the Vermont exit", "101 freeway at Vermont"),
    ("la_homeless", "Someone sleeping outside needs help, Venice", "+13105550136",
     "There's a man sleeping outside my building and he seems like he needs help", "Venice, on Rose Avenue", "Rose Avenue, Venice"),
    ("county_coyote", "Coyote in the yard, Altadena", "+16265550151",
     "There's a coyote walking around my backyard and I have small kids", "Altadena, near Lake Avenue", "Lake Avenue, Altadena"),
    ("county_noise", "Neighbors yelling, Hacienda Heights", "+16265550166",
     "My neighbors have been yelling and slamming doors for hours", "Hacienda Heights", "Hacienda Heights"),
    ("lb_tree", "Tree branch down, Long Beach", "+15625550142",
     "A big branch fell off the street tree and it's blocking the sidewalk", "Long Beach, Belmont Shore", "2nd Street, Belmont Shore, Long Beach"),
    ("lb_dog", "Loose dog, Long Beach", "+15625550175",
     "There's a loose dog running in and out of traffic", "Long Beach, on Atlantic", "Atlantic Avenue, Long Beach"),
    ("pas_car", "Abandoned car, Pasadena", "+16265550188",
     "Someone left a beat up car with no plates in front of my house", "Pasadena, on Lake Avenue", "Lake Avenue, Pasadena"),
    ("sm_graffiti", "Tagging on a wall, Santa Monica", "+13105550152",
     "Someone tagged the wall of the alley behind my apartment", "Santa Monica, near Lincoln", "Lincoln Blvd, Santa Monica"),
    ("ing_light", "Streetlight out, Inglewood", "+13105550164",
     "The light on my corner has been out for a week, it's really dark", "Inglewood, on Manchester", "Manchester Blvd, Inglewood"),
    ("compton_couch", "Old sofa to get rid of, Compton", "+13105550171",
     "I need someone to haul away an old sofa and a broken dresser", "Compton, on Rosecrans", "Rosecrans Avenue, Compton"),
    ("weho_parking", "Car blocking driveway, West Hollywood", "+13235550158",
     "Someone parked across my driveway and I can't get out", "West Hollywood, on Fountain", "Fountain Avenue, West Hollywood"),
    ("downey_dumping", "Junk dumped in alley, Downey", "+15625550123",
     "Somebody dumped a pile of tires and trash in the alley behind us", "Downey, off Firestone", "Firestone Blvd, Downey"),
    ("la_ticket", "\"I need to pay a ticket\"", "+12135550144",
     "Hi, I need to pay a ticket", "It's in Los Angeles, I got it downtown", "Downtown Los Angeles"),
    ("la_rent", "\"I can't pay my rent this month\"", "+13235550119",
     "I can't pay my rent this month, is there any help", "Boyle Heights", "Boyle Heights"),
    ("la_roaches", "Roaches and a landlord who won't act", "+12135550166",
     "There's roaches all over my apartment and my landlord won't do anything about it", "Koreatown, on Kenmore", "Kenmore Avenue, Koreatown"),
    ("lb_barking", "Dog barking all night, Long Beach", "+15625550191",
     "My neighbor's dog barks all night long, every night", "Long Beach, Bixby Knolls", "Bixby Knolls, Long Beach"),
    ("pas_trash", "Trash wasn't picked up, Pasadena", "+16265550124",
     "They skipped our street, the trash bins are still full from Monday", "Pasadena, on Orange Grove", "Orange Grove Blvd, Pasadena"),
    ("la_sidewalk", "Tree roots lifted the sidewalk, Mid-City", "+13235550153",
     "The sidewalk is all lifted up from tree roots, my mom tripped on it", "Mid-City, on Pico", "Pico Blvd, Mid-City"),
    ("ing_breakin", "Car broken into overnight, Inglewood", "+13105550186",
     "Someone smashed my car window last night and took my laptop", "Inglewood, near Market Street", "Market Street, Inglewood"),
    ("weho_build", "Neighbor building without a permit, West Hollywood", "+13235550112",
     "My neighbor is building a second story addition and I don't think they have a permit", "West Hollywood, on Larrabee", "Larrabee Street, West Hollywood"),
    ("la_food", "Sick after eating out", "+12135550129",
     "My whole family got sick after eating at a restaurant last night", "Los Angeles, Chinatown", "Chinatown"),
    ("la_brush", "Dry brush behind the house, Palisades", "+13105550197",
     "The hillside behind our house is full of dead brush, it's a fire risk", "Pacific Palisades", "Pacific Palisades"),
    ("la_signal", "Stop sign knocked down, Westchester", "+13105550115",
     "Someone knocked over the stop sign at the corner, cars are blowing through", "Westchester, near Manchester", "Manchester Avenue, Westchester"),
    ("la_sewer", "Sewage smell and overflow, Wilmington", "+13105550188",
     "There's sewage bubbling up out of the manhole and it smells awful", "Wilmington, on Avalon", "Avalon Blvd, Wilmington"),
    ("la_dead_cat", "Dead cat in the street, Sun Valley", "+18185550158",
     "There's a dead cat in the street in front of my house", "Sun Valley", "Sun Valley"),
    ("glendora_pothole", "Pothole, Glendora (searched live)", "+16265550133",
     "There's a pothole so deep it popped my tire", "Glendora, on Foothill", "Foothill Blvd, Glendora"),
    ("sd_library", "Library hours, San Diego (new service)", "+16195550120",
     "Is the central library open today", "San Diego, downtown", "Central Library, downtown San Diego"),
    ("burbank_bulky", "Old fridge to get rid of, Burbank", "+18185550139",
     "How do I get rid of an old refrigerator", "Burbank, on Glenoaks", "Glenoaks Blvd, Burbank"),
]
for _id, _title, _num, _said, _where, _loc in _MORE:
    SCENARIOS[_id] = {
        "title": _title, "caller": _num, "service": "", "location": _loc,
        "script": [("agent", GREETING), ("caller", _said), ("agent", "I can help with that. Where is this?"),
                   ("caller", _where)],
    }

# Demo picker: any incident x any location.
INCIDENTS = {
    "pothole": "There's a pothole so deep it popped my tire",
    "dead_animal": "There's a dead possum on the sidewalk in front of my house",
    "coyote": "There's a coyote walking around my backyard",
    "snake": "There's a snake in my garden",
    "couch": "I need someone to haul away an old couch and a mattress",
    "abandoned_car": "There's a car with flat tires that hasn't moved in a month",
    "graffiti": "Someone spray painted tags all over the wall by my building",
    "streetlight": "The streetlight on my corner has been out for a week",
    "signal": "The traffic light at the intersection is completely dark",
    "party": "My neighbors are blasting music and it's two in the morning",
    "barking": "My neighbor's dog barks all night, every night",
    "tree": "A big branch fell off the street tree and it's blocking the sidewalk",
    "dumping": "Somebody dumped a pile of tires and trash in the alley",
    "roaches": "There's roaches all over my apartment and my landlord won't do anything",
    "rent": "I can't pay my rent this month, is there any help",
    "ticket": "Hi, I need to pay a parking ticket",
    "water_main": "Water is gushing out of the street, it's flooding the intersection",
    "break_in": "Someone smashed my car window last night and took my laptop",
    "food": "My whole family got sick after eating at a restaurant last night",
    "library": "Is the public library open today",
    "gas": "I smell gas really strong outside my neighbor's house",
}
# (label, caller number for the area-code guess, what they say, location string)
LOCATIONS = {
    "hollywood": ("Hollywood (City of LA)", "+13235550142", "Hollywood, near Sunset and Vine", "Sunset and Vine, Hollywood"),
    "van_nuys": ("Van Nuys (City of LA)", "+18185550199", "Van Nuys, on Sherman Way", "Sherman Way, Van Nuys"),
    "santa_monica": ("Santa Monica (its own city)", "+13105550152", "Santa Monica, near Lincoln", "Lincoln Blvd, Santa Monica"),
    "pasadena": ("Pasadena (its own city)", "+16265550188", "Pasadena, on Lake Avenue", "Lake Avenue, Pasadena"),
    "long_beach": ("Long Beach (its own city)", "+15625550142", "Long Beach, Belmont Shore", "2nd Street, Belmont Shore, Long Beach"),
    "topanga": ("Topanga (unincorporated county)", "+13105550161", "Topanga, off Topanga Canyon Boulevard", "Topanga"),
    "east_la": ("East LA (unincorporated county)", "+13235550163", "East LA, Whittier and Atlantic", "Whittier Blvd and Atlantic, East Los Angeles"),
    "glendora": ("Glendora (not learned yet)", "+16265550133", "Glendora, on Foothill", "Foothill Blvd, Glendora"),
    "san_diego": ("San Diego (wildcard)", "+16195550120", "San Diego, in North Park", "North Park, San Diego"),
    "irvine": ("Irvine (wildcard)", "+19495550170", "Irvine, near the Spectrum", "Irvine Spectrum, Irvine"),
}


STYLES = {
    "normal": "Caller: normal",
    "slow": "Caller asks Rosa to slow down",
    "hurry": "Caller is in a hurry",
}


def build_scenario(incident: str, location: str, style: str = "normal") -> dict:
    label, number, where, loc = LOCATIONS[location]
    said = INCIDENTS[incident]
    script = [("agent", GREETING)]
    if style == "slow":
        script += [("caller", "I'm sorry, you're talking way too fast for me, can you slow down?"),
                   ("agent", "Of course. I'll slow down. What can I help you with?")]
    if style == "hurry":
        said += ", please hurry, I need this taken care of right now"
    script += [("caller", said), ("agent", "I can help with that. Where is this?"), ("caller", where)]
    return {"title": f"{incident} in {label} ({style})", "caller": number, "service": "", "location": loc,
            "script": script}


# The demo reel: a spread of calls back to back.
# A breadth tour: different services, jurisdictions, and behaviors (voice
# speed, urgency, emergency, live web search, a brand-new service).
REEL = ["slow_down", "la_graffiti", "lb_tree", "la_roaches", "county_coyote", "glendora_pothole", "pas_car",
        "hurry_up", "la_ticket", "ing_breakin", "compton_couch", "la_rent", "weho_build", "gas",
        "la_food", "sd_library", "lb_barking", "downey_dumping"]


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
        time.sleep(random.uniform(0.11, 0.24))
    _pace_rules(call_id, text)
    flow.schedule_system1(call_id, _caller_text(call_id), delay=0.2, latest=text,
                          on_result=lambda r: _act(call_id, r))
    time.sleep(0.9)


# The same reactions a live call gets (agent.py), minus the Guava commands:
# the dashboard shows exactly what a live call would.
SPEEDS = ["x-slow", "slow", "medium", "fast", "x-fast"]
_state: dict[str, dict] = {}


def _set_speed(call_id: str, new: str, why: str, kind: str = "speed", by_caller: bool = True) -> None:
    st = _state.setdefault(call_id, {"speed": "medium"})
    if st["speed"] == new or (kind == "urgent" and st.get("by_caller")):
        return
    bus.note(call_id, "voice.speed", {"from": st["speed"], "to": new, "why": why})
    bus.annotate(call_id, f"Voice speed {st['speed']} → {new}: {why}", kind)
    st.update(speed=new, by_caller=st.get("by_caller") or by_caller)
    bus._update(call_id, "call", speech_speed=new, **({"urgent": why} if kind == "urgent" else {}))


def _pace_rules(call_id: str, text: str) -> None:
    from .agent import FASTER, SLOWER
    st = _state.setdefault(call_id, {"speed": "medium"})
    i = SPEEDS.index(st["speed"])
    if SLOWER.search(text):
        st["rule_line"] = text
        _set_speed(call_id, SPEEDS[max(0, i - 2)], f'caller said "{text}"')
    elif FASTER.search(text):
        st["rule_line"] = text
        _set_speed(call_id, SPEEDS[min(4, i + 1)], f'caller said "{text}"')


def _act(call_id: str, r: dict) -> None:
    st = _state.setdefault(call_id, {"speed": "medium"})
    if r["urgency"] >= 2.0:
        _set_speed(call_id, "fast", f"urgent (System One urgency {r['urgency']:.1f} of 3): fast, short sentences",
                   kind="urgent", by_caller=False)
    latest = r.get("latest") or ""
    if r["pace"] in ("slower", "faster") and r["pace_p"] >= 0.8 and st.get("rule_line") != latest:
        i = SPEEDS.index(st["speed"])
        new = SPEEDS[max(0, i - 2)] if r["pace"] == "slower" else SPEEDS[min(4, i + 1)]
        _set_speed(call_id, new, f'System One heard "{latest}" as a request to talk {r["pace"]} ({r["pace_p"]:.0%})')


def run_scenario(name: str | dict) -> str:
    sc = SCENARIOS[name] if isinstance(name, str) else name
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


_reel_running = threading.Event()


def run_reel(gap: float = 6.0) -> bool:
    """Play the demo reel, one call after another. Returns False if already playing."""
    if _reel_running.is_set():
        return False
    _reel_running.set()

    def go() -> None:
        try:
            for name in REEL:
                if not _reel_running.is_set():
                    break
                cid = run_scenario(name)
                # wait for this call to finish before starting the next
                for _ in range(240):
                    c = bus.get(cid) or {}
                    if c.get("ended_at"):
                        break
                    time.sleep(0.5)
                time.sleep(gap)
        finally:
            _reel_running.clear()

    threading.Thread(target=go, daemon=True, name="demo-reel").start()
    return True


def stop_reel() -> None:
    _reel_running.clear()
