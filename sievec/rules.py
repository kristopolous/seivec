"""The decision model: (service category x jurisdiction) -> department.

Routing is a table lookup, not an LLM judgment. The fuzzy part is only
figuring out the category and the jurisdiction:

  * category: keyword scoring first (instant, deterministic). Only if the
    keywords are unsure does the LLM pick from the fixed category list.
  * jurisdiction: see places.py.

The table lives in data/rules.json (City of LA, unincorporated LA County).
Cities learned from the web are cached in data/learned/<slug>.json and show
up as extra columns. Operator corrections add caller phrases to a category's
keywords (data/learned_phrases.json), so the fast path improves right away.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("sievec.rules")

DATA = Path(__file__).resolve().parent.parent / "data"
RULES_PATH = DATA / "rules.json"
DEPTS_PATH = DATA / "departments.json"
LEARNED_DIR = DATA / "learned"          # runtime cache (git-ignored)
SEED_DIR = DATA / "seed_cities"          # pre-learned cities shipped with the repo
PHRASES_PATH = DATA / "learned_phrases.json"
SERVICES_PATH = DATA / "learned_services.json"   # services discovered on calls (e.g. "Library")
FEEDBACK_PATH = DATA / "feedback.jsonl"

STOPWORDS = set("""
a an the and or of to in on at for from with by is are was were be been it its this that there
my our your their me we you i im i'm can could would should please need want get got have has
just like some so very about up out over into down near front back side street st ave avenue
call calling help hi hello yes yeah no ok okay thanks thank um uh hey well really
""".split())

DEPT_FIELDS = ("id", "jurisdiction", "department", "service", "phone", "phone_display",
               "hours", "description", "notes", "source_url", "verified")


# Danger words. A match flags the call as an emergency no matter how the
# service ranking comes out; phrase lists alone miss "my house IS on fire".
EMERGENCY = re.compile(
    r"\b(on fire|catch(ing)? fire|caught fire|(house|building|car|apartment)\b.{0,15}\bfire|smoke (is )?(coming|everywhere|pouring)|"
    r"bleeding|unconscious|not breathing|can'?t breathe|heart attack|stroke|overdos(e|ing)|"
    r"gun|shot|shooting|stabb(ed|ing)|being attacked|attacking (me|us|someone)|break[- ]?in|breaking in|"
    r"smell(s|ing)? (of )?gas|gas leak|power line(s)? (is |are )?(down|sparking)|downed (power )?line|"
    r"(someone|somebody|person|people|he|she|they)('s| is| are| got)? (hurt|injured|trapped)|"
    r"crash with injur|hit by a (car|truck)|drowning|explosion)",
    re.I,
)


def emergency_in(text: str) -> str | None:
    m = EMERGENCY.search(text or "")
    return m.group(0) if m else None


def tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9']+", text.lower()):
        if w in STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class Rules:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reload()

    # -- loading ------------------------------------------------------------

    def reload(self) -> None:
        with self._lock:
            raw = json.loads(RULES_PATH.read_text())
            self.departments = {d["id"]: d for d in json.loads(DEPTS_PATH.read_text())}
            self.categories = {c["id"]: c for c in raw["categories"]}
            self.label_to_id = {c["label"].lower(): c["id"] for c in raw["categories"]}
            self.jurisdictions = dict(raw["jurisdictions"])
            self.table = {cid: dict(cells) for cid, cells in raw["table"].items()}
            self.learned_cells: dict[str, dict[str, dict]] = {}
            LEARNED_DIR.mkdir(parents=True, exist_ok=True)
            if not any(LEARNED_DIR.glob("*.json")):
                for f in SEED_DIR.glob("*.json"):
                    (LEARNED_DIR / f.name).write_text(f.read_text())
            for f in sorted(LEARNED_DIR.glob("*.json")):
                self._load_learned(json.loads(f.read_text()))
            self.learned_phrases = json.loads(PHRASES_PATH.read_text()) if PHRASES_PATH.exists() else {}
            # Services discovered during calls: new rows with their own keywords and cells.
            self.services = (json.loads(SERVICES_PATH.read_text()) if SERVICES_PATH.exists()
                             else {"categories": {}, "cells": {}})
            for cid, c in self.services["categories"].items():
                self.categories.setdefault(cid, c)
                self.label_to_id.setdefault(c["label"].lower(), cid)
            self._build_index()

    def _load_learned(self, doc: dict) -> None:
        key = doc["key"]
        self.jurisdictions[key] = {"name": doc["name"], "short": doc.get("short", doc["name"]),
                                   "kind": "learned", "learned_at": doc.get("learned_at"),
                                   "complete": doc.get("complete", False)}
        self.learned_cells[key] = doc.get("cells", {})

    def _build_index(self) -> None:
        self._index = {}
        for cid, c in self.categories.items():
            phrases = [k.lower() for k in c["keywords"]] + [c["label"].lower()]
            phrases += [p.lower() for p in self.learned_phrases.get(cid, [])]
            self._index[cid] = [(p, set(tokens(p))) for p in phrases if p]

    # -- System 1: keyword ranking -------------------------------------------

    def rank(self, text: str, limit: int = 4) -> list[dict[str, Any]]:
        lowered = " " + re.sub(r"\s+", " ", text.lower()) + " "
        toks = set(tokens(text))
        if not toks:
            return []
        scores: dict[str, float] = {}
        for cid, phrases in self._index.items():
            s = 0.0
            for phrase, ptoks in phrases:
                if not ptoks:
                    continue
                if f" {phrase} " in lowered or (len(phrase) > 6 and phrase in lowered):
                    s += 2.0 * len(ptoks)
                elif not self.categories[cid].get("emergency"):
                    # Partial credit for overlapping words. Never for emergencies:
                    # "someone" alone must not light up "someone is hurt".
                    hit = len(ptoks & toks)
                    if hit:
                        s += 0.6 * hit / len(ptoks) * min(len(ptoks), 2)
            if cid == "general":
                s *= 0.4  # the catch-all should only win when nothing else does
            if s:
                scores[cid] = s
        if not scores:
            return []
        top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        total = sum(v for _, v in top)
        best = top[0][1]
        return [{
            "id": cid,
            "label": self.categories[cid]["label"],
            "emergency": self.categories[cid].get("emergency", False),
            "raw": round(s, 2),
            "score": round((s / total) * min(1.0, best / 4.0) ** 0.35, 3),
        } for cid, s in top]

    def pick_category(self, text: str, hint_label: str | None = None) -> dict[str, Any]:
        """Category for a request. Returns {id, label, method, lanes}."""
        if hint_label and hint_label.lower() in self.label_to_id:
            cid = self.label_to_id[hint_label.lower()]
            return {"id": cid, "label": self.categories[cid]["label"], "method": "caller confirmed (Guava multiple choice)",
                    "lanes": self.rank(text)}
        lanes = self.rank(text)
        if lanes:
            top = lanes[0]
            second = lanes[1]["raw"] if len(lanes) > 1 else 0.0
            if top["raw"] >= 2.0 and top["raw"] >= 1.6 * second:
                return {"id": top["id"], "label": top["label"], "method": "keyword rule", "lanes": lanes}
        cid = self._llm_category(text, lanes)
        return {"id": cid, "label": self.categories[cid]["label"],
                "method": "System One (keywords unsure)", "lanes": lanes}

    def systemone_questions(self) -> dict[str, Any]:
        """Everything System One decides about a caller's words, in one call."""
        return {
            "service": {"type": "choice", "instructions": "Which city or county service does the caller need?",
                        "criteria": {cid: f"{c['label']}: {c.get('description', '')}"[:200]
                                     for cid, c in self.categories.items()}},
            "emergency": {"type": "noul", "instructions": "Is anyone in danger right now (fire, injury, crime in "
                                                          "progress, gas leak, downed power line)?"},
            "wants_human": {"type": "noul", "instructions": "Is the caller asking to talk to a real person instead "
                                                            "of an automated assistant?"},
            "urgency": {"type": "score", "instructions": "How urgent is the caller's problem?",
                        "criteria": ["routine", "soon", "urgent", "life-threatening"]},
            "pace": {"type": "choice", "instructions": "In the caller's LATEST line only: are they asking the "
                                                       "assistant itself to talk slower or faster? Ignore requests to "
                                                       "send help quickly.",
                     "criteria": {"none": "No request about how fast the assistant talks",
                                  "slower": "Asked the assistant to slow down its speech",
                                  "faster": "Asked the assistant to talk faster"}},
        }

    def ask_systemone(self, text: str, latest: str | None = None) -> dict[str, Any]:
        """One System One call: service probabilities plus the safety signals."""
        from .llm import systemone
        started = time.time()
        state = f"Caller so far: {text[-1500:]}" + (f"\nCaller's latest line: {latest}" if latest else "")
        a = systemone(state, self.systemone_questions(), purpose="System One: read the caller")
        probs = sorted(a["service"]["probabilities"].items(), key=lambda kv: -kv[1])
        lanes = [{"id": cid, "label": self.categories[cid]["label"], "score": round(p, 3), "raw": round(p * 10, 2),
                  "emergency": self.categories[cid].get("emergency", False)}
                 for cid, p in probs[:4] if cid in self.categories and p >= 0.01]
        return {
            "id": a["service"]["choice"], "label": self.categories.get(a["service"]["choice"], {}).get("label"),
            "confidence": round(a["service"]["confidence"], 3), "lanes": lanes,
            "emergency": round(a["emergency"]["noul"], 3), "wants_human": round(a["wants_human"]["noul"], 3),
            "urgency": round(a["urgency"]["score"], 2), "pace": a["pace"]["choice"],
            "pace_p": round(max(a["pace"]["probabilities"].values()), 3),
            "latency_ms": int((time.time() - started) * 1000), "latest": latest,
        }

    def _llm_category(self, text: str, lanes: list[dict]) -> str:
        try:
            s1 = self.ask_systemone(text)
            if s1["id"] in self.categories:
                return s1["id"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("System One failed, falling back to chat: %s", exc)
        from .llm import complete_json
        options = "\n".join(f"- {c['id']}: {c['label']} ({c['description']})" for c in self.categories.values())
        prompt = (
            "Classify a caller's request to an LA city/county services line into exactly one category id.\n"
            f"Categories:\n{options}\n\nKeyword guesses (may be wrong): {[l['id'] for l in lanes]}\n"
            f"Caller said: {text[-800:]}"
        )
        schema = {"type": "object", "properties": {"category_id": {"type": "string", "enum": list(self.categories)}},
                  "required": ["category_id"]}
        try:
            out, _ = complete_json(prompt, schema, system=1, purpose="System 1: service category")
            cid = out.get("category_id")
            if cid in self.categories:
                return cid
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM category fallback failed: %s", exc)
        return lanes[0]["id"] if lanes else "general"

    # -- the table -----------------------------------------------------------

    def dept(self, dept_id: str | None) -> dict[str, Any] | None:
        d = self.departments.get(dept_id or "")
        return {k: d.get(k) for k in DEPT_FIELDS} if d else None

    def lookup(self, category: str, jurisdiction: str) -> dict[str, Any] | None:
        with self._lock:
            found = self.services["cells"].get(category, {}).get(jurisdiction)
            if found:
                return dict(found)
            if jurisdiction in self.learned_cells:
                cell = self.learned_cells[jurisdiction].get(category)
                return dict(cell) if cell and cell.get("phone") else None
            return self.dept(self.table.get(category, {}).get(jurisdiction))

    def save_learned_cells(self, key: str, name: str, cells: dict[str, dict], complete: bool = False) -> None:
        with self._lock:
            path = LEARNED_DIR / f"{key.split(':', 1)[-1]}.json"
            doc = json.loads(path.read_text()) if path.exists() else {
                "key": key, "name": name, "short": name.replace("City of ", ""),
                "learned_at": time.time(), "cells": {}}
            doc["cells"].update(cells)
            doc["complete"] = doc.get("complete") or complete
            LEARNED_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(doc, indent=1))
            self._load_learned(doc)

    def add_service(self, cid: str, label: str, keywords: list[str], description: str,
                    jurisdiction: str, cell: dict[str, Any]) -> None:
        """A new row in the rules, discovered on a call, with its first cell."""
        with self._lock:
            cat = self.services["categories"].get(cid) or {
                "id": cid, "label": label, "emergency": False, "description": description,
                "keywords": [], "discovered": True, "discovered_at": time.time()}
            cat["keywords"] = list(dict.fromkeys(cat["keywords"] + [k.lower() for k in keywords if k]))
            self.services["categories"][cid] = cat
            self.services["cells"].setdefault(cid, {})[jurisdiction] = cell
            SERVICES_PATH.write_text(json.dumps(self.services, indent=1))
            self.categories[cid] = cat
            self.label_to_id[label.lower()] = cid
            self._build_index()

    def ensure_jurisdiction(self, key: str, name: str) -> None:
        with self._lock:
            if key not in self.jurisdictions:
                self.jurisdictions[key] = {"name": name, "short": name.replace("City of ", ""), "kind": "learned"}
                self.learned_cells.setdefault(key, {})

    def public_table(self) -> dict[str, Any]:
        with self._lock:
            cols = list(self.jurisdictions)
            rows = []
            for cid, c in self.categories.items():
                cells = {}
                for j in cols:
                    cell = self.services["cells"].get(cid, {}).get(j)
                    if cell is None and j in self.learned_cells:
                        cell = self.learned_cells[j].get(cid)
                    elif cell is None:
                        cell = self.dept(self.table.get(cid, {}).get(j))
                    if cell:
                        cells[j] = {"department": cell.get("department"), "service": cell.get("service"),
                                    "phone_display": cell.get("phone_display"), "learned": j in self.learned_cells,
                                    "verified": cell.get("verified")}
                rows.append({"id": cid, "label": c["label"], "emergency": c.get("emergency", False),
                             "discovered": c.get("discovered", False), "cells": cells})
            return {"columns": [{"key": j, **self.jurisdictions[j]} for j in cols], "rows": rows}

    # -- feedback ------------------------------------------------------------

    def record_feedback(self, row: dict[str, Any]) -> dict[str, Any]:
        """Store a review. A correction teaches the fast path the caller's phrase."""
        learned = None
        with self._lock:
            DATA.mkdir(exist_ok=True)
            with FEEDBACK_PATH.open("a") as f:
                f.write(json.dumps({**row, "ts": time.time()}) + "\n")
            phrase = (row.get("phrase") or "").strip().lower()
            if row.get("correct") != row.get("predicted") and phrase:
                bucket = self.learned_phrases.setdefault(row["correct"], [])
                if phrase not in bucket:
                    bucket.append(phrase)
                    PHRASES_PATH.write_text(json.dumps(self.learned_phrases, indent=1))
                    self._build_index()
                    learned = {"category": row["correct"], "phrase": phrase}
        return {"learned": learned}

    def feedback_stats(self) -> dict[str, Any]:
        latest: dict[str, dict] = {}
        if FEEDBACK_PATH.exists():
            for line in FEEDBACK_PATH.read_text().splitlines():
                if line.strip():
                    r = json.loads(line)
                    latest[r.get("call_id", str(len(latest)))] = r
        rows = list(latest.values())
        good = sum(1 for r in rows if r.get("correct") == r.get("predicted"))
        return {
            "reviewed": len(rows),
            "correct": good,
            "accuracy": round(good / len(rows), 3) if rows else None,
            "learned_phrases": sum(len(v) for v in self.learned_phrases.values()),
        }


rules = Rules()
