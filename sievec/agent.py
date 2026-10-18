"""The Guava voice agent. Guava runs the conversation; Sievec makes the decision."""

from __future__ import annotations

import logging
import re
import time

import guava
from guava.events import AgentSpeechEvent, BotSessionEnded, CallerSpeechEvent, EscalateEvent
from guava.types.call_info import PSTNCallInfo, WebRTCCallInfo

from . import flow, history, places
from .bus import bus
from .rules import emergency_in, rules

logger = logging.getLogger("sievec.agent")

agent = guava.Agent(
    name="Rosa",
    organization="Sievec, the Los Angeles city and county services line",
    purpose=(
        "Answer one number for every non-emergency city and county service. Based in Los Angeles, but it looks up "
        "the right office for any city, anywhere. Confirm where the problem is, find out what the caller needs, "
        "and connect them to the office that handles it."
    ),
    # Say "Los Angeles", never "L.A.": the voice reads "LA" as Louisiana.
    pronunciations={"Sievec": "civix", "LA": "Los Angeles", "L.A.": "Los Angeles", "LADOT": "L A dot",
                    "LADWP": "L A D W P", "StreetsLA": "Streets L A", "LASAN": "L A san",
                    # Digit by digit, not "nine hundred eleven".
                    "911": "nine one one", "9-1-1": "nine one one", "311": "three one one",
                    "3-1-1": "three one one", "211": "two one one", "2-1-1": "two one one"},
)


def caller_text(call_id: str) -> str:
    snap = bus.get(call_id) or {}
    return " ".join(l["text"] for l in snap.get("transcript", []) if l["role"] == "caller")


@agent.on_call_start
def on_call_start(call: guava.Call) -> None:
    info = call.call_info
    if isinstance(info, PSTNCallInfo):
        channel, number = "phone", info.from_number
    elif isinstance(info, WebRTCCallInfo):
        channel, number = "browser", (info.custom_payload or {}).get("pretend_number")
    else:
        channel, number = "sip", None
    guess = places.guess_from_number(number)
    call.set_variable("guess", guess)
    bus.call_started(call.id, channel, number, guess=guess)
    if guess["key"]:
        bus.step(call.id, "area", "done", guess["area_code"], f"{guess['region']}: guessing {guess['name']}",
                 data=guess)
        where = (f"The caller's area code ({guess['area_code']}) suggests {guess['name']}. Confirm it, e.g. "
                 f"\"Is this in {guess['name']}?\" If they say another city or neighborhood, record that instead. "
                 "Also get cross streets or a landmark.")
    else:
        bus.step(call.id, "area", "done", guess["area_code"] or "none", "No area code hint, asking the caller")
        where = "Ask which city or neighborhood the problem is in, plus cross streets or a landmark."

    call.set_task(
        "intake",
        objective=(
            "Greet the caller as Sievec, the Los Angeles city and county services line. Learn what they need and "
            "where. Keep it short and warm. If they already said the problem, don't make them repeat it. "
            "If they mention an accident, a fall, or anything where someone could be hurt, ask whether anyone "
            "is injured; if so, tell them to hang up and dial nine-one-one now. Never promise when a crew will arrive. "
            "Always say \"Los Angeles\" in full, never \"L.A.\" "
            "Never tell a caller you can't help because of where they are: if the problem is in another city "
            "(even outside California), just record that city; the system looks up the right office there."
        ),
        checklist=[
            guava.Field(
                key="service",
                description="What the caller needs help with. Pick the closest service from the search results.",
                field_type="multiple_choice",
                searchable=True,
            ),
            guava.Field(key="location", description=where),
        ],
        completion_criteria="You know the service and where the problem is.",
    )


@agent.on_search_query("service")
def search_service(call: guava.Call, query: str):
    """Guava asks us for options; System 1 keyword ranking answers instantly."""
    lanes = rules.rank(f"{query} {caller_text(call.id)}")
    matches = [l["label"] for l in lanes]
    common = ["Pothole or road damage", "Bulky item pickup", "Dead animal pickup", "Streetlight out",
              "Traffic signal or stop sign", "Something else"]
    return matches, [c for c in common if c not in matches]


SPEEDS = ["x-slow", "slow", "medium", "fast", "x-fast"]
# "too fast"/"too slow" only count when aimed at Rosa: "cars are going too fast" is a traffic report.
SLOWER = re.compile(r"\b(slow (it )?down(?! (the|those|these|cars|traffic|drivers|people))|(talk|speak)(ing)? (more )?slow(er|ly)|"
                    r"you('?re| are)? (talking |speaking |going )?too fast|"
                    r"more slowly|slower,? please)\b", re.I)
FASTER = re.compile(r"\b(hurry( it)? up|speed (it )?up|(talk|speak) faster|you('?re| are)? (talking |speaking |going |being )?too slow|"
                    r"faster,? please|pick up the pace)\b", re.I)


URGENT = re.compile(r"\b(urgent|asap|right now|immediately|emergency|hurry|water (is )?(everywhere|pouring|gushing)|"
                    r"flood(ing|ed)|blocking (the )?(road|street|traffic|lane)|can'?t get out|stuck|dangerous)\b", re.I)


def set_urgent(call: guava.Call, why: str) -> None:
    """Urgent calls get a quicker, shorter Rosa, unless the caller asked her to slow down."""
    if call.get_variable("urgent") or call.get_variable("speed_by_caller"):
        return
    call.set_variable("urgent", why)
    call.set_variable("speech_speed", "fast")
    call.set_persona(speech_speed="fast")
    call.send_instruction("This is urgent for the caller. Keep every sentence short and get to the point.")
    bus.note(call.id, "voice.urgent", {"why": why, "speech_speed": "fast"})
    bus.annotate(call.id, f"Urgent ({why}): Rosa switched to fast, short sentences.", "urgent")
    bus._update(call.id, "call", speech_speed="fast", urgent=why)


def adjust_speed(call: guava.Call, event: CallerSpeechEvent) -> None:
    """"Slow down" / "hurry up": one step on Guava's speech_speed scale per request."""
    step = -1 if SLOWER.search(event.utterance) else 1 if FASTER.search(event.utterance) else 0
    if not step or call.get_variable("speed_uid") == event.utterance_id:
        return  # partial transcripts repeat the same utterance; act once
    call.set_variable("speed_uid", event.utterance_id)
    call.set_variable("pace_rule_at", time.time())
    current = call.get_variable("speech_speed") or "medium"
    new = SPEEDS[max(0, min(len(SPEEDS) - 1, SPEEDS.index(current) + step))]
    if new == current:
        return
    call.set_variable("speech_speed", new)
    call.set_variable("speed_by_caller", True)
    call.set_persona(speech_speed=new)
    bus.note(call.id, "voice.speed", {"from": current, "to": new, "caller_said": event.utterance})
    bus.annotate(call.id, f"Voice speed {current} → {new}: caller said \"{event.utterance.strip()}\"", "speed")
    bus._update(call.id, "call", speech_speed=new)


WANTS_HUMAN = re.compile(
    r"\b(hate (ai|a\.i\.|robots?|bots?|this (robot|bot|ai|machine|thing))|(talk|speak) to (a |an )?(real |actual |live )?(person|human|someone)|"
    r"real person|human being|representative|operator|not a (robot|bot|machine)|stop (being )?(a )?robot|"
    r"is this (a |an )?(robot|bot|ai|recording))\b", re.I)


def escalate_to_311(call: guava.Call, why: str) -> None:
    """Fallback for callers who don't want an AI: forward them to 311 for
    their place (MyLA311 for Los Angeles, or the other city's general line)."""
    if call.get_variable("escalated"):
        return
    call.set_variable("escalated", why)
    snap = bus.get(call.id) or {}
    place = (snap.get("decision") or {}).get("place") or {}
    key = place.get("key") or (call.get_variable("guess") or {}).get("key") or "la_city"
    if key.startswith("city:") and rules.lookup("general", key):
        cell, where = rules.lookup("general", key), place.get("name", "their city")
    else:
        cell, where = rules.lookup("general", "la_city"), "Los Angeles"
    decision = {"category": {"id": "general", "label": "A person at 311", "method": f"caller asked for a human ({why})"},
                "place": place or {"key": "la_city", "name": "Los Angeles"}, "cell": cell,
                "rule": f"Caller wants a person → 311 ({where})", "escalated": True}
    bus.annotate(call.id, f"Caller asked for a person ({why}). Forwarding to {cell['department']}, "
                          f"{cell['phone_display']}.", "urgent")
    bus.step(call.id, "rule", "done", cell["department"], decision["rule"])
    target = flow.transfer_target(cell)
    bus.step(call.id, "connect", "active", cell.get("phone_display"), "Forwarding to a person")
    bus.status(call.id, "transferring", decision=decision,
               transfer={"mode": flow.TRANSFER_MODE, "to": target, "department_phone": cell.get("phone")})
    line = (f"Say: \"No problem. I'll connect you with a person at 311. Their number is {cell['phone_display']} "
            f"if we get cut off.\"")
    if target:
        call.transfer(target, line + " Then transfer.")
    else:
        call.hangup(line + " Then end the call politely.")


def act_on_systemone(call: guava.Call, r: dict) -> None:
    """System One's read of the caller, turned into actions. The phrase rules
    already fired instantly on the words; this catches what they miss."""
    if call.get_variable("escalated"):
        return
    if r["emergency"] >= 0.85:
        set_urgent(call, f"System One: danger {r['emergency']:.2f}")
        mark_emergency(call, f"System One: danger probability {r['emergency']:.2f}", warn=True)
        return
    if r["wants_human"] >= 0.9:
        escalate_to_311(call, f"System One: wants a person {r['wants_human']:.2f}")
        return
    if r["urgency"] >= 2.0:
        set_urgent(call, f"System One: urgency {r['urgency']:.1f} of 3")
    latest = r.get("latest") or ""
    if (r["pace"] in ("slower", "faster") and r["pace_p"] >= 0.8
            and call.get_variable("pace_line") != latest
            and time.time() - (call.get_variable("pace_rule_at") or 0) > 6):  # the phrase rule just did it
        call.set_variable("pace_line", latest)
        step = -1 if r["pace"] == "slower" else 1
        current = call.get_variable("speech_speed") or "medium"
        new = SPEEDS[max(0, min(len(SPEEDS) - 1, SPEEDS.index(current) + step))]
        if new != current:
            call.set_variable("speech_speed", new)
            call.set_variable("speed_by_caller", True)
            call.set_persona(speech_speed=new)
            bus.note(call.id, "voice.speed", {"from": current, "to": new, "by": "System One", "p": r["pace_p"]})
            bus.annotate(call.id, f"Voice speed {current} → {new}: System One heard \"{latest.strip()}\" "
                                  f"as a request to talk {r['pace']} ({r['pace_p']:.0%}).", "speed")
            bus._update(call.id, "call", speech_speed=new)


@agent.on_escalate
def on_escalate(call: guava.Call, event: EscalateEvent) -> None:
    escalate_to_311(call, "Guava escalation: requested by the caller" if event.requested_by == "human"
                    else "Guava escalation: agent couldn't help")


def mark_emergency(call: guava.Call, why: str, warn: bool) -> None:
    """Flag the call as an emergency (once). The call is logged as 911 even if
    the caller simply hangs up to go dial it."""
    if call.get_variable("emergency"):
        return
    call.set_variable("emergency", why)
    bus.alert(call.id, f"Emergency: {why}. Caller told to hang up and dial 911.")
    bus.annotate(call.id, f"Emergency flagged ({why}). Logged as a 911 call.", "emergency")
    bus.step(call.id, "rule", "done", "Emergency", f"Danger detected: {why}")
    bus.step(call.id, "connect", "done", "Dial 911", "Never transferred")
    bus.status(call.id, "emergency")
    if warn:
        call.send_instruction(
            "The caller may be describing an emergency. Calmly tell them to hang up and dial nine-one-one now. "
            "If they clearly say it's not an emergency, keep helping them."
        )


@agent.on_caller_speech
def on_caller_speech(call: guava.Call, event: CallerSpeechEvent) -> None:
    bus.speech(call.id, "caller", event.utterance, event.utterance_id)
    text = caller_text(call.id)
    lanes = rules.rank(text)
    bus.lanes(call.id, lanes)
    flow.schedule_system1(call.id, text, latest=event.utterance,
                          on_result=lambda r: act_on_systemone(call, r))
    adjust_speed(call, event)
    if (h := WANTS_HUMAN.search(event.utterance)) and not emergency_in(event.utterance):
        escalate_to_311(call, f'caller said "{h.group(0)}"')
        return
    danger = emergency_in(event.utterance)
    if danger:
        set_urgent(call, f'emergency: "{danger}"')
        mark_emergency(call, f'caller said "{danger}"', warn=True)
    elif (m := URGENT.search(event.utterance)) and not FASTER.search(event.utterance):
        set_urgent(call, f'caller said "{m.group(0)}"')


@agent.on_agent_speech
def on_agent_speech(call: guava.Call, event: AgentSpeechEvent) -> None:
    bus.speech(call.id, "agent", event.utterance, interrupted=event.interrupted)
    # Rosa can send a caller to 911 on her own (the task tells her to when
    # someone is hurt). If she does, the call is an emergency.
    if re.search(r"\b(9-?1-?1|nine[- ]one[- ]one)\b", event.utterance, re.I) and re.search(
            r"\b(dial|call|hang up)\b", event.utterance, re.I):
        mark_emergency(call, "agent sent the caller to 911", warn=False)


@agent.on_question
def on_question(call: guava.Call, question: str) -> str:
    return flow.answer_question(call.id, question)


@agent.on_task_complete("intake")
def on_intake_done(call: guava.Call) -> None:
    snap = bus.get(call.id) or {}
    transcript = "\n".join(f"{l['role']}: {l['text']}" for l in snap.get("transcript", []))
    outcome = flow.decide(
        call.id,
        call.get_field("service") or "",
        call.get_field("location") or "",
        transcript,
        call.get_variable("guess") or {},
        say=call.send_instruction,
    )
    if outcome.action == "transfer" and outcome.target:
        call.transfer(outcome.target, outcome.instructions)
    else:
        call.hangup(outcome.instructions)


@agent.on_session_end
def on_session_end(call: guava.Call, event: BotSessionEnded) -> None:
    snap = bus.get(call.id) or {}
    if event.termination_reason == "bot-transfer":
        bus.step(call.id, "connect", "done", None, "Connected")
        bus.status(call.id, "transferred")
    elif call.get_variable("emergency"):
        pass  # stays "emergency"; bus.ended keeps it
    elif snap.get("status") == "transferring" and (flow.TRANSFER_MODE == "handoff" or call.get_variable("escalated")):
        cell = (snap.get("decision") or {}).get("cell") or {}
        bus.step(call.id, "connect", "done", cell.get("phone_display"), "Handoff briefing ready")
        bus.status(call.id, "handoff")
    bus.ended(call.id, event.termination_reason)
    history.fetch_after_call(call.id)


def run(phone: str | None, webrtc_code: str | None) -> None:
    """Blocks. Must run on the main thread: Runner installs signal handlers."""
    runner = guava.Runner()
    if phone:
        runner.listen_phone(agent, phone)
    if webrtc_code:
        runner.listen_webrtc(agent, webrtc_code)
    runner.run()
