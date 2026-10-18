"""The Guava voice agent. Guava runs the conversation; Sievec makes the decision."""

from __future__ import annotations

import logging

import guava
from guava.events import AgentSpeechEvent, BotSessionEnded, CallerSpeechEvent
from guava.types.call_info import PSTNCallInfo, WebRTCCallInfo

from . import flow, history, places
from .bus import bus
from .rules import rules

logger = logging.getLogger("sievec.agent")

agent = guava.Agent(
    name="Rosa",
    organization="Sievec, the Los Angeles city and county services line",
    purpose=(
        "Answer one number for every non-emergency city and county service in Los Angeles County. "
        "Confirm where the problem is, find out what the caller needs, and connect them to the office that handles it."
    ),
    pronunciations={"Sievec": "SIV-ick", "LADOT": "L A dot", "LADWP": "L A D W P", "StreetsLA": "Streets L A"},
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
            "Greet the caller as Sievec, the LA city and county services line. Learn what they need and "
            "where. Keep it short and warm. If they already said the problem, don't make them repeat it. "
            "Never promise when a crew will arrive."
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


@agent.on_caller_speech
def on_caller_speech(call: guava.Call, event: CallerSpeechEvent) -> None:
    bus.speech(call.id, "caller", event.utterance, event.utterance_id)
    text = caller_text(call.id)
    lanes = rules.rank(text)
    bus.lanes(call.id, lanes)
    flow.schedule_system1(call.id, text)
    if lanes and lanes[0]["emergency"] and lanes[0]["raw"] >= 2 and not call.get_variable("emergency_warned"):
        call.set_variable("emergency_warned", True)
        bus.alert(call.id, "Possible emergency. Agent told the caller to dial 911.")
        call.send_instruction(
            "The caller may be describing an emergency. Calmly tell them that if anyone is in danger, or there's "
            "fire, a gas smell, or a downed power line, they should hang up and dial 911 now. If they say it's "
            "not an emergency, keep helping them."
        )


@agent.on_agent_speech
def on_agent_speech(call: guava.Call, event: AgentSpeechEvent) -> None:
    bus.speech(call.id, "agent", event.utterance, interrupted=event.interrupted)


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
    elif snap.get("status") == "transferring" and flow.TRANSFER_MODE == "handoff":
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
