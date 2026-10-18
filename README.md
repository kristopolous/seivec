<p align="center"><img src="static/civit.png" alt="Sievec civet logo" width="110"></p>

# Sievec

**Routes you right when time is tight.**

One phone number for every non-emergency city and county service in Los Angeles County. You call and say "there's a dead possum on my sidewalk" or "the stoplight at Figueroa and 7th is out". Sievec works out which office handles it and where you are, then forwards you there with a briefing already prepared for the person who picks up.

Built on [Guava](https://goguava.ai) for the voice layer: speech, turn-taking, structured fields and transfers.

## How a call is routed

The routing is a **decision table**, not an LLM judgment. Fast rules decide first, and models only settle what the rules can't.

1. **Area code.** The caller's number gives a first guess at the jurisdiction (213/323 → City of LA, 626 → San Gabriel Valley…). The agent confirms it with the caller.
2. **Location.** Resolved from a cache of LA neighborhoods, unincorporated communities and the county's 88 cities. Places it doesn't know are looked up with a web search.
3. **Service.** Keyword rules score every caller utterance instantly. When they're unsure, a fast "System 1" model (`nimble`) picks from the fixed list of 31 services. Guava's searchable multiple-choice field confirms the choice with the caller.
4. **Rule.** `(service × jurisdiction) → department`, read from `data/rules.json`.
5. **Connect.** The agent says: *"Ok great, I'm going to forward you to LA Sanitation. I'll tell them what's going on and transfer the call."* Sievec then produces the briefing the outbound leg would speak to the department.

**Cities it hasn't seen.** If a caller is in, say, Glendale, Sievec:
- searches the web for the city's department directory (Parallel search),
- reads the pages (Parallel extract) and collects every phone number on them with its label,
- has a "System 2" model (`qwen3.8`) map those numbers onto the 31 services.

The phone numbers are copied from the pages, never generated. The new column is cached, so the next Glendale caller gets an instant table lookup.

## The dashboard

- **The board.** Every department and number on file, about 160 to start, narrows live as the call goes on: the area code first, then the confirmed location, then what the caller says. Cities being learned fill in as you watch.
- **The freeway sign.** The current routing decision.
- **Pipeline.** The five steps, each showing how it was decided (cache, web, keyword rule, System 1, caller confirmed).
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
| `SIEVEC_LLM_URL` | Ollama-compatible endpoint (default `https://9ol.es/11434`) |
| `SIEVEC_S1_MODEL` / `SIEVEC_S2_MODEL` | Fast / slow models (default `nimble` / `qwen3.8`) |
| `SIEVEC_TRANSFER` | `handoff` (default: end with the briefing), `demo` (transfer to `SIEVEC_DEMO_NUMBER`), `live` (transfer to the real department) |
| `SIEVEC_WARM_CITIES` | Cities to learn at startup, e.g. `Pasadena,Burbank` |

## Data

- `data/departments.json` has 39 LA City, LA County, utility and regional entries, each with a source URL. Entries with `verified: false` could not be confirmed on an official page.
- `data/rules.json` is the decision table: 31 services × City of LA / unincorporated LA County.
- `data/seed_cities/` holds pre-learned cities. "Forget cities learned on calls" resets to this set.

## Layout

```
main.py             entry point: dashboard thread + Guava runner
sievec/agent.py     Guava agent: intake task, searchable service field, handlers
sievec/flow.py      the five-step routing pipeline (shared by live and sample calls)
sievec/rules.py     decision table, keyword ranking, System 1 fallback, feedback
sievec/places.py    area codes, place cache, web lookup
sievec/learn.py     learn a city's column from its own web pages
sievec/llm.py       System 1 / System 2 model calls, payload tracing
sievec/websearch.py Parallel search + extract
sievec/audit.py     hash-chained audit log
sievec/bus.py       live call state, streamed to the dashboard
static/             dashboard (no build step)
```
