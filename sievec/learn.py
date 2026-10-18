"""Learn a whole decision-table column for a city we've never seen.

    web search for the city's department directory (Parallel)
    -> pull full page text (Parallel extract)
    -> every phone number on those pages, with the words around it
    -> System 2 assigns snippets to our service categories
    -> cells cached in data/learned/<city>.json

The model only picks snippet numbers; the phone number itself is copied from
the page, so it can't invent one.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from urllib.parse import urlparse

from . import websearch
from .llm import complete_json, current_call
from .rules import rules

logger = logging.getLogger("sievec.learn")

PHONE = re.compile(r"\(?\b(\d{3})\)?[-. ]?(\d{3})[-. ](\d{4})\b")
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="learn")
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()

Progress = Callable[[dict[str, Any]], None]


def _clean(t: str) -> str:
    t = re.sub(r"\]\([^)]*\)", "]", t)          # drop markdown link targets
    t = re.sub(r"[*#|\[\]_`>]+", " ", t)
    return re.sub(r"\s+", " ", t).strip(" :-,;")


def _snippets(pages: list[dict], limit: int = 80) -> list[dict]:
    """Every phone number with the text since the previous number (before) and
    up to the next one (after). Directory pages put labels on either side."""
    seen: dict[str, dict] = {}
    for p in pages:
        text = re.sub(r"\s+", " ", p["content"])
        matches = list(PHONE.finditer(text))
        for i, m in enumerate(matches):
            digits = "".join(m.groups())
            if digits in seen:
                continue
            prev_end = matches[i - 1].end() if i else max(0, m.start() - 160)
            next_start = matches[i + 1].start() if i + 1 < len(matches) else m.end() + 160
            before = _clean(text[max(prev_end, m.start() - 110): m.start()])[-90:]
            after = _clean(text[m.end(): min(next_start, m.end() + 110)])[:90]
            # Which side is this number's own label on? Decide from the markup.
            raw_before, raw_after = text[max(0, m.start() - 4): m.start()], text[m.end(): m.end() + 8]
            heading = re.search(r"(#{1,6}|\*\*)[^#*]{2,60}(\*\*)?\s*\[?\(?$", text[max(prev_end, m.start() - 70): m.start()])
            if "|" in raw_before:
                label = before                       # table: "| Label | number |"
            elif heading:
                label = before                       # "### Non-Emergency [(626) 914-8250](tel:..)"
            elif raw_after.startswith(("](tel", ")](tel")):
                label = after                        # link list: "[number](tel:..) Label"
            else:
                label = f"{before} [NUMBER] {after}"
            seen[digits] = {"phone": f"+1{digits}", "phone_display": f"({digits[:3]}) {digits[3:6]}-{digits[6:]}",
                            "label": label, "context": f"{before} [NUMBER] {after}", "url": p["url"]}
    return list(seen.values())[:limit]


def _official_first(results: list[dict], city: str) -> list[str]:
    slug = re.sub(r"[^a-z]", "", city.lower())
    def score(r: dict) -> int:
        host = urlparse(r["url"] or "").netloc.lower()
        return (2 if slug in host.replace("-", "") else 0) + (1 if host.endswith((".gov", ".org", ".us")) else 0)
    urls = []
    for r in sorted(results, key=score, reverse=True):
        if r["url"] and r["url"] not in urls:
            urls.append(r["url"])
    return urls


def learn_city(key: str, city: str, focus: str | None, progress: Progress) -> dict[str, dict]:
    """Fill every cell we can for `city`. Returns {category: cell}."""
    with _guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        if (rules.jurisdictions.get(key) or {}).get("complete"):
            return dict(rules.learned_cells.get(key, {}))
        rules.ensure_jurisdiction(key, f"City of {city}")
        cats = list(rules.categories.values())
        emit = lambda cid, state, **kw: progress({"key": key, "city": city, "category": cid,  # noqa: E731
                                                 "label": rules.categories[cid]["label"], "state": state, **kw})
        for c in cats:
            emit(c["id"], "queued")

        focus_label = rules.categories[focus]["label"].lower() if focus else "city services"
        for c in cats:
            emit(c["id"], "searching")
        results = websearch.search(
            f"Official City of {city}, California department phone directory: contact numbers for public works, "
            f"street lights, trash and bulky items, animal control, police non-emergency, code enforcement, and "
            f"{focus_label}.",
            [f"City of {city} CA contact us department phone numbers",
             f"City of {city} CA {focus_label} phone",
             f"City of {city} public works phone"],
            max_results=6, purpose=f"learn {city}: find directory pages",
        )
        urls = _official_first(results, city)[:3]
        for c in cats:
            emit(c["id"], "reading", sources=urls)
        pages = websearch.extract(urls, f"Department names and phone numbers for City of {city} services",
                                  purpose=f"learn {city}: read pages")
        snips = _snippets(pages)
        if not snips:
            for c in cats:
                emit(c["id"], "failed", reason="no phone numbers on the pages")
            return {}

        listing = "\n".join(f"[{i}] {s['phone_display']}: {s['label']}" for i, s in enumerate(snips))
        options = "\n".join(f"- {c['id']}: {c['label']} ({c['description']})" for c in cats if not c.get("emergency"))
        schema = {"type": "object", "properties": {
            "assignments": {"type": "array", "description": "One entry per category you can fill",
                            "items": {"type": "object", "properties": {
                                "category_id": {"type": "string"},
                                "snippet": {"type": "integer", "description": "Index of the snippet with the number"},
                                "department": {"type": "string", "description": "Office name, from the snippet"},
                                "note": {"type": "string"}}}},
            "city_hall": {"type": "integer", "description": "Snippet index of the main city hall / general line"},
        }}
        try:
            out, _ = complete_json(
                f"These are phone numbers found on official City of {city} pages, each with its label. Where the "
                f"label is unclear you see the surrounding text with [NUMBER] marking the number's position.\n"
                f"{listing}\n\nFor each service category below, pick the number a resident should call, only when "
                f"that number's own label clearly fits the service. A wrong number is worse than none: skip "
                f"categories without a clear match. Use 'general' for the main city hall or information line.\n\n"
                f"Categories:\n{options}",
                schema, system=2, purpose=f"learn {city}: map numbers to services",
            )
        except Exception as exc:  # noqa: BLE001
            for c in cats:
                emit(c["id"], "failed", reason=str(exc))
            return {}

        filled: dict[str, dict] = {}
        assignments = list(out.get("assignments") or [])
        if isinstance(out.get("city_hall"), int):
            assignments.append({"category_id": "general", "snippet": out["city_hall"], "department": f"City of {city}"})
        for a in assignments:
            cid, idx = a.get("category_id"), a.get("snippet")
            if cid not in rules.categories or rules.categories[cid].get("emergency") or cid in filled:
                continue
            if not isinstance(idx, int) or not 0 <= idx < len(snips):
                continue
            s = snips[idx]
            cell = {
                "id": f"{key}:{cid}", "jurisdiction": key,
                "department": (a.get("department") or f"City of {city}").strip()[:80],
                "service": rules.categories[cid]["label"],
                "phone": s["phone"], "phone_display": s["phone_display"],
                "hours": "", "notes": (a.get("note") or "")[:160],
                "description": f"Learned from {urlparse(s['url']).netloc}.",
                "source_url": s["url"], "source_text": s["context"], "verified": False, "learned": True,
            }
            filled[cid] = cell
        # Targeted "<place> <service> phone number" results win over the directory crawl.
        existing = rules.learned_cells.get(key, {})
        filled = {cid: c for cid, c in filled.items() if cid not in existing}
        rules.save_learned_cells(key, f"City of {city}", filled, complete=True)
        for c in cats:
            if c["id"] in filled:
                emit(c["id"], "done", cell=filled[c["id"]])
            else:
                emit(c["id"], "failed", reason="not listed")
        return filled


# What a resident would type into a search box for each service.
SEARCH_TERMS = {
    "dead_animal": "dead animal pickup", "live_animal": "animal control", "barking_dog": "barking dog complaint",
    "bulky_item": "bulky item pickup", "missed_trash": "missed trash pickup", "illegal_dumping": "illegal dumping",
    "pothole": "pothole repair", "sidewalk": "sidewalk repair", "tree": "street tree trimming",
    "traffic_signal": "traffic signal repair", "streetlight": "streetlight repair", "sewer_flooding": "sewer storm drain",
    "graffiti": "graffiti removal", "abandoned_vehicle": "abandoned vehicle", "parking": "parking enforcement",
    "parking_ticket": "parking citation", "noise": "noise complaint", "police_report": "police non-emergency",
    "homeless": "homeless outreach", "renter": "tenant landlord help", "code_violation": "code enforcement",
    "power_water": "water utility", "restaurant": "restaurant health complaint", "food_illness": "food poisoning report",
    "mosquitoes": "vector control mosquitoes", "freeway": "caltrans", "fire_hazard": "fire department non-emergency",
    "social_services": "social services", "general": "311 city services",
}


def search_query(city: str, category: str) -> str:
    """"<place> <service> phone number", e.g. "Glendora bulky item pickup phone number"."""
    term = SEARCH_TERMS.get(category) or rules.categories[category]["label"].lower()
    return f"{city} {term} phone number"


def learn_service(key: str, city: str, category: str, progress: Progress) -> dict | None:
    """Search "<place> <service> phone number", read the number, store the cell.

    The number comes from the search excerpts when they have one; otherwise
    from the top pages' full text. Either way it's copied, not generated."""
    cat = rules.categories[category]
    label = cat["label"].lower()
    query = search_query(city, category)
    emit = lambda state, **kw: progress({"key": key, "city": city, "category": category,  # noqa: E731
                                        "label": cat["label"], "state": state, "query": query, **kw})
    emit("searching")
    rules.ensure_jurisdiction(key, f"City of {city}")
    results = websearch.search(
        f"The official City of {city}, California phone number a resident calls for {label}.",
        [query, f"City of {city} CA {SEARCH_TERMS.get(category, label)} contact"],
        max_results=5, purpose=f'search "{query}"',
    )
    # Only trust results about this city: its own site, or pages that name it.
    # (A search for Glendora happily returns Glendale pages.)
    slug_ = re.sub(r"[^a-z]", "", city.lower())
    results = [r for r in results
               if slug_ in re.sub(r"[^a-z]", "", urlparse(r["url"] or "").netloc.lower())
               or re.search(rf"\b{re.escape(city)}\b", f"{r.get('title') or ''} {r['text']}", re.I)]
    urls = _official_first(results, city)[:3]
    emit("reading", sources=urls)
    excerpt_pages = [{"url": r["url"], "content": r["text"]} for r in results if r["url"] in urls]
    snips = _snippets(excerpt_pages)
    if not snips:
        snips = _snippets(websearch.extract(urls, f"Phone number for {label} in City of {city}",
                                            purpose=f"read pages for {city} {label}"))
    if not snips:
        emit("failed", reason="no phone numbers in the results")
        return None
    listing = "\n".join(f"[{i}] {s['phone_display']}: {s['label']}" for i, s in enumerate(snips))
    schema = {"type": "object", "properties": {
        "snippet": {"type": "integer", "description": "Index of the right number, or -1 if none clearly fits"},
        "department": {"type": "string", "description": "Office name, from the snippet"},
        "note": {"type": "string"}}}
    try:
        out, _ = complete_json(
            f"Phone numbers from official City of {city} pages, each with its label:\n{listing}\n\n"
            f"Which one should a resident call for: {cat['label']} ({cat['description']})? Answer -1 unless the "
            f"number's own label clearly fits."
            + ("" if category == "general" else " Don't pick a general city hall line."),
            schema, system=2, purpose=f"learn {city}: pick {cat['label']} number",
        )
    except Exception as exc:  # noqa: BLE001
        emit("failed", reason=str(exc))
        return None
    idx = out.get("snippet")
    if not isinstance(idx, int) or not 0 <= idx < len(snips):
        emit("failed", reason="no clear match on the pages")
        return None
    s = snips[idx]
    cell = {
        "id": f"{key}:{category}", "jurisdiction": key,
        "department": (out.get("department") or f"City of {city}").strip()[:80],
        "service": cat["label"], "phone": s["phone"], "phone_display": s["phone_display"],
        "hours": "", "notes": (out.get("note") or "")[:160],
        "description": f"Learned from {urlparse(s['url']).netloc}.",
        "source_url": s["url"], "source_text": s["context"], "verified": False, "learned": True,
        "query": query, "learned_at": time.time(),
    }
    rules.save_learned_cells(key, f"City of {city}", {category: cell})
    emit("done", cell=cell)
    return cell


def learn_in_background(key: str, city: str, progress: Progress, call_id: str | None = None) -> None:
    def job() -> None:
        token = current_call.set(call_id)
        try:
            learn_city(key, city, None, progress)
        except Exception:  # noqa: BLE001
            logger.exception("learning %s failed", city)
        finally:
            current_call.reset(token)
    _pool.submit(job)


def warm_cities(cities: list[str], progress: Progress) -> None:
    """Pre-learn cities before a demo so the board starts big."""
    from .places import city_key
    for city in cities:
        learn_in_background(city_key(city), city, progress)
        time.sleep(0.2)


def _place_words(key: str | None, name: str) -> str:
    if key == "la_city":
        return "Los Angeles"
    if key == "la_county":
        return "Los Angeles County"
    return name.replace("City of ", "") if name else "Los Angeles"


def discover(need: str, key: str | None, place_name: str, progress: Progress,
             question: str | None = None) -> dict[str, Any] | None:
    """Anything the table doesn't cover: figure out the search, run it, and if
    there's a phone number, use it and keep it as a new service row.

    Returns {category, label, cell, query, answer} or None."""
    from .rules import slug
    place = _place_words(key, place_name)
    existing = ", ".join(f"{c['id']}" for c in rules.categories.values())
    try:
        plan, _ = complete_json(
            f"A caller to the Los Angeles city and county services line needs something our routing table "
            f"doesn't cover.\nWhat they said: {need}\n" + (f"Their question: {question}\n" if question else "")
            + f"Where: {place}\n\nName the service they need in a few words, give 4-8 short phrases a caller "
            f"might use for it, and write the web search a person would type to find the phone number of the "
            f"office that handles it, in the form '<place> <service> phone number' (be specific: include the "
            f"facility or branch if they named one).\nExisting service ids (reuse one only if it truly fits): "
            f"{existing}",
            {"type": "object", "properties": {
                "service_id": {"type": "string", "description": "snake_case, e.g. library"},
                "service_label": {"type": "string", "description": "e.g. Library"},
                "description": {"type": "string"},
                "keywords": {"type": "array", "items": {"type": "string"}},
                "query": {"type": "string"}}},
            system=2, purpose="plan a search for an unlisted service",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("discover plan failed: %s", exc)
        return None
    cid = slug(plan.get("service_id") or plan.get("service_label") or "service")
    label = (plan.get("service_label") or cid.replace("_", " ")).strip().capitalize()
    query = (plan.get("query") or f"{place} {label} phone number").strip()
    if cid in rules.categories and not rules.categories[cid].get("discovered"):
        return {"category": cid, "label": rules.categories[cid]["label"], "cell": None, "query": query, "answer": None,
                "existing": True}
    col = key or "la_city"
    emit = lambda state, **kw: progress({"key": col, "city": place, "category": cid,  # noqa: E731
                                        "label": label, "state": state, "query": query, **kw})
    emit("searching")
    results = websearch.search(
        f"{question or need} Find the official phone number for: {label}, {place}.",
        [query, f"{place} {label} contact"], max_results=5, purpose=f'search "{query}"')
    if key and key.startswith("city:"):
        city = place
        results = [r for r in results if re.search(rf"\b{re.escape(city)}\b", f"{r.get('title') or ''} {r['text']} {r['url']}", re.I)
                   or re.sub(r"[^a-z]", "", city.lower()) in (r["url"] or "").lower()]
    urls = [r["url"] for r in results][:3]
    emit("reading", sources=urls)
    snips = _snippets([{"url": r["url"], "content": r["text"]} for r in results])
    pages_text = "\n\n".join(f"SOURCE {r['url']}\n{r['text'][:900]}" for r in results[:3])
    if not snips:
        pages = websearch.extract(urls, f"Phone number for {label} in {place}", purpose=f"read pages for {label}")
        snips = _snippets(pages)
        pages_text = "\n\n".join(f"SOURCE {p['url']}\n{p['content'][:1500]}" for p in pages[:2]) or pages_text
    if not snips:
        emit("failed", reason="no phone numbers in the results")
        return None
    listing = "\n".join(f"[{i}] {s['phone_display']}: {s['label']}" for i, s in enumerate(snips))
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%A %B %-d, %Y %-I:%M %p")
    try:
        out, _ = complete_json(
            f"Phone numbers found for '{label}' in {place}, each with its label:\n{listing}\n\n"
            f"Page text:\n{pages_text}\n\nPick the number a resident should call for {label} "
            f"(-1 if none clearly fits) and name the office."
            + (f" Also answer the caller's question in one or two short spoken sentences, using only the page "
               f"text. It is {now} in Los Angeles right now: compare against today's hours and say plainly whether "
               f"it's open right now or already closed (and when it next opens, if the pages say). If the pages don't "
               f"answer it, say you couldn't find it. Question: {question}"
               if question else ""),
            {"type": "object", "properties": {
                "snippet": {"type": "integer"}, "department": {"type": "string"},
                "answer": {"type": "string"}}},
            system=2, purpose=f"pick the {label} number",
        )
    except Exception as exc:  # noqa: BLE001
        emit("failed", reason=str(exc))
        return None
    idx = out.get("snippet")
    if not isinstance(idx, int) or not 0 <= idx < len(snips):
        emit("failed", reason="no clear match")
        return {"category": cid, "label": label, "cell": None, "query": query, "answer": out.get("answer")}
    s = snips[idx]
    cell = {
        "id": f"{col}:{cid}", "jurisdiction": col,
        "department": (out.get("department") or label).strip()[:80], "service": label,
        "phone": s["phone"], "phone_display": s["phone_display"], "hours": "", "notes": "",
        "description": f"Found by searching \"{query}\".", "source_url": s["url"], "source_text": s["context"],
        "verified": False, "learned": True, "query": query, "learned_at": time.time(),
    }
    keywords = [k for k in (plan.get("keywords") or []) if isinstance(k, str)][:8] + [label.lower()]
    rules.add_service(cid, label, keywords, plan.get("description") or label, col, cell)
    emit("done", cell=cell)
    return {"category": cid, "label": label, "cell": cell, "query": query, "answer": out.get("answer")}
