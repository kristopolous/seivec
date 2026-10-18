<p align="center"><img src="static/civit.png" alt="Sievec civet logo" width="110"></p>

# Sievec

**Routes you right when time is tight.**

One phone number for every non-emergency city and county service. You call and say "there's a dead possum on my sidewalk" or "the stoplight at Figueroa and 7th is out". Sievec works out which office handles it and where you are, then forwards you there with a briefing already prepared for the person who picks up. It starts with Los Angeles City and County and looks up any other city on the fly.

## What's used

| Piece | What it does in Sievec |
| - | - |
| **[Guava](https://goguava.ai)** (Python SDK) | The whole voice layer: phone and browser (WebRTC) lines, speech recognition, turn-taking, the intake task, a searchable multiple-choice field that confirms the service with the caller, `on_question`, `on_escalate`, `set_persona(speech_speed=…)` for voice speed, pronunciations, transfers, and the per-call transcript and recording API. |
| **Ollama System One**, model `nimble` ([`POST /v1/systemone`](https://docs.ollama.com/api/systemone)) | The fast decision model. One call per caller utterance (about 1 s) asks typed questions about everything said so far and gets probabilities back: **service** (a `choice` over every service in the rules), **emergency** (`noul`), **wants a real person** (`noul`), **urgency** (`score` 0–3) and **pace** (`choice`: slower / faster / none, judged on the latest line only). The service probabilities drive the live "what the caller is asking for" bars; the other answers trigger actions (below). |
| **`qwen3.8`** via Ollama `/api/chat` | The slow "System 2" model. Used only when reading the web: placing an unknown location, writing the search for a service the rules don't have, picking the right number out of a page's phone list, and answering callers' questions from page text. |
| **[Parallel](https://parallel.ai)** search + extract | Web search for places, cities and services that aren't in the rules yet, and full-page text when the search excerpts don't include a phone number. |

Every request to System One, `qwen3.8` and Parallel shows up with its full payload under **Under the hood → Payloads** on the dashboard, and in the audit trail.

## How a call is routed

The routing is a **decision table**, not an LLM judgment. Fast rules decide first, System One settles what the rules can't, and System 2 is only used to read the web.

1. **Area code.** The caller's number gives a first guess at the jurisdiction (213/323 → City of Los Angeles, 626 → San Gabriel Valley…). Rosa confirms it with the caller.
2. **Location.** Resolved from a cache of Los Angeles neighborhoods, unincorporated communities and the county's 88 cities, with fuzzy matching for misheard names ("Landora" → Glendora). Anything else, anywhere, is placed with a web search.
3. **Service.** Keyword rules score every caller utterance instantly. When they're unsure, **System One** picks from the services in the rules. Guava's searchable multiple-choice field confirms the choice with the caller.
4. **Rule.** `(service × jurisdiction) → department`, read from `data/rules.json` plus everything learned on earlier calls.
5. **Connect.** Rosa says: *"Ok great, I'm going to forward you to LA Sanitation. I'll tell them what's going on and transfer the call."* Sievec then produces the briefing the outbound leg would speak to the department.

**Missing rules are filled from the web, then kept.** When the place or the service isn't in the rules:
- **A city we haven't seen (Glendora, San Francisco…):** search `"<city> <service> phone number"`, read the number from the results, and store it as that `(service × city)` cell. If there's no direct line, search `"<city> 311 city services phone number"` for the city's general line.
- **A service the table doesn't have (the library, say):** System 2 writes the search a person would type ("Los Angeles Central Library phone number"). Sievec reads the number and adds the service as a **new row** in the rules with its own keywords, so the next caller is a plain lookup.
- **Results must be about the right city** (a Glendora search also returns Glendale pages), and the phone number is always copied from the page, never generated.

**Safety and comfort signals.** Instant phrase rules act on the caller's words; System One backs them up with probabilities:

| Signal | Phrase rule | System One | Action |
| - | - | - | - |
| Emergency | "on fire", "smell gas", "someone is hurt", … | `emergency` ≥ 0.85 | Tell the caller to hang up and dial 911; logged as a 911 call |
| Wants a person | "I hate AI", "real person", "operator", … | `wants_human` ≥ 0.9 | Forward to 311 for their place (Guava `on_escalate` too) |
| Urgent | "right now", "flooding", "blocking traffic", … | `urgency` ≥ 2 of 3 | Faster voice, shorter sentences |
| Pace | "slow down", "talk faster", … | `pace` = slower/faster ≥ 0.8 | Step Guava's `speech_speed` (x-slow … x-fast) |

Each of these shows as a note in the transcript at the moment it happened.

## The dashboard

- **The board.** Every department and number on file, about 160 to start, narrows live as the call goes on: the area code first, then the confirmed location, then what the caller says. Cities being learned fill in as you watch.
- **The freeway sign.** The current routing decision.
- **Pipeline.** The five steps, each showing how it was decided (cache, web search, keyword rule, System One, caller confirmed).
- **System One readout.** The latest System One answer: service and confidence, danger, wants a person, urgency, pace.
- **Call log.** Grouped by live / today / earlier, with an outcome badge per call and a transcript search box. Past calls show Guava's transcript and recording.
- **Stats.** How many calls went to each department and service, by jurisdiction and by how each decision was made.
- **Handoff briefing.** The exact words of the outbound call. It can be played aloud.
- **Review.** Mark a routing right or wrong. A correction teaches the keyword rules the caller's phrase, so the next caller who says it routes instantly.
- **Under the hood.** Every model and web request with its full payload, plus the audit trail.

## Audit trail

Every step (area-code guess, place resolution and its sources, service choice and method, rule hit, handoff briefing, model and web requests, operator reviews) is appended to `data/audit.jsonl`. Each record carries the SHA-256 of the record before it, so any edit breaks the chain. `GET /api/audit/verify` checks it, and `GET /api/calls/{id}/audit` exports one call.

## Run it

```bash
# needs uv and Python 3.11+
cp .env.example .env        # fill in keys
uv sync

uv run main.py --sim-only   # dashboard + scripted sample calls, no Guava needed
uv run main.py --no-phone   # live: talk to it from the browser ("Call from browser")
guava run                   # live: phone number (GUAVA_AGENT_NUMBER) + browser
```

Open **http://localhost:8311**.

| Variable | Meaning |
| - | - |
| `GUAVA_API_KEY` | Guava API key (live calls) |
| `GUAVA_AGENT_NUMBER` | Phone number to answer on |
| `PARALLEL_API_KEY` | Web search for unknown places and cities |
| `SIEVEC_LLM_URL` | Ollama endpoint for System One (`/v1/systemone`) and chat (`/api/chat`); default `https://9ol.es/11434` |
| `SIEVEC_S1_MODEL` | System One model (default `nimble`) |
| `SIEVEC_S2_MODEL` | System 2 model for reading the web (default `qwen3.8`) |
| `SIEVEC_TRANSFER` | `handoff` (default: end with the briefing), `demo` (transfer to `SIEVEC_DEMO_NUMBER`), `live` (transfer to the real department) |
| `SIEVEC_WARM_CITIES` | Cities to learn at startup, e.g. `Pasadena,Burbank` |

## Data

- `data/departments.json` has 39 LA City, LA County, utility and regional entries, each with a source URL. Entries with `verified: false` could not be confirmed on an official page.
- `data/rules.json` is the decision table: 31 services × City of LA / unincorporated LA County.
- `data/seed_cities/` holds pre-learned cities. "Forget cities learned on calls" resets to this set.
- Learned at runtime (git-ignored): `data/learned/` (city cells), `data/learned_services.json` (new service rows), `data/learned_phrases.json` (phrases from operator corrections), `data/calls.jsonl`, `data/audit.jsonl`.

## Layout

```
main.py             entry point: dashboard thread + Guava runner
sievec/agent.py     Guava agent: intake task, searchable service field, handlers
sievec/flow.py      the five-step routing pipeline (shared by live and sample calls)
sievec/rules.py     decision table, keyword ranking, System One questions, feedback
sievec/places.py    area codes, place cache, web lookup
sievec/learn.py     fill missing rules from the web: city cells and new services
sievec/llm.py       System One (/v1/systemone) and System 2 (/api/chat) calls, payload tracing
sievec/history.py   past calls, Guava transcripts and recordings
sievec/websearch.py Parallel search + extract
sievec/audit.py     hash-chained audit log
sievec/bus.py       live call state, streamed to the dashboard
static/             dashboard (no build step)
```
