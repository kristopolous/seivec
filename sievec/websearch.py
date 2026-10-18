"""Parallel web search, used only for places and cities we haven't cached."""

from __future__ import annotations

import logging
import os
import time

import httpx

from .llm import trace

logger = logging.getLogger("sievec.websearch")

URL = "https://api.parallel.ai/v1beta/search"


def available() -> bool:
    return bool(os.environ.get("PARALLEL_API_KEY"))


def search(objective: str, queries: list[str], max_results: int = 3, max_chars: int = 2500,
           purpose: str = "web search") -> list[dict]:
    """Return [{url, title, text}]; empty list on any failure."""
    if not available():
        return []
    body = {"objective": objective, "search_queries": queries, "max_results": max_results}
    started = time.time()
    try:
        r = httpx.post(
            URL,
            headers={"x-api-key": os.environ["PARALLEL_API_KEY"], "parallel-beta": "search-extract-2025-10-10"},
            json=body,
            timeout=20.0,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        trace("web", purpose, {"url": URL, **body}, {"error": str(exc)}, started, model="parallel", error=True)
        logger.warning("Parallel search failed: %s", exc)
        return []
    out, budget = [], max_chars
    for res in data.get("results", []):
        text = " ".join(res.get("excerpts") or [])[: max(0, budget)]
        budget -= len(text)
        out.append({"url": res.get("url"), "title": res.get("title"), "text": text})
    trace("web", purpose, {"url": URL, **body},
          {"search_id": data.get("search_id"),
           "results": [{"url": o["url"], "title": o["title"], "excerpt": o["text"][:400]} for o in out]},
          started, model="parallel")
    return out


EXTRACT_URL = "https://api.parallel.ai/v1beta/extract"


def extract(urls: list[str], objective: str, purpose: str = "web extract") -> list[dict]:
    """Full page text for each URL: [{url, title, content}]."""
    if not available() or not urls:
        return []
    body = {"urls": urls, "objective": objective, "excerpts": False, "full_content": True}
    started = time.time()
    try:
        r = httpx.post(
            EXTRACT_URL,
            headers={"x-api-key": os.environ["PARALLEL_API_KEY"], "parallel-beta": "search-extract-2025-10-10"},
            json=body,
            timeout=40.0,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        trace("web", purpose, {"url": EXTRACT_URL, **body}, {"error": str(exc)}, started, model="parallel", error=True)
        return []
    out = [{"url": x.get("url"), "title": (x.get("title") or "").strip(), "content": x.get("full_content") or ""}
           for x in data.get("results", [])]
    trace("web", purpose, {"url": EXTRACT_URL, **body},
          {"extract_id": data.get("extract_id"),
           "results": [{"url": o["url"], "title": o["title"], "chars": len(o["content"])} for o in out]},
          started, model="parallel")
    return out
