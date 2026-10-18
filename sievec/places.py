"""Where is the problem? Area-code guess -> caller confirms -> resolve.

Resolution order:
  1. Known names: LA City neighborhoods, unincorporated communities, the
     county's 88 cities (no network).
  2. Web search + LLM extraction for anything else.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any

from .rules import rules, slug

logger = logging.getLogger("sievec.places")

# Area code -> what we'll guess before asking. Deliberately coarse: area codes
# cross city lines, so the caller always confirms.
AREA_CODES = {
    "213": ("la_city", "the City of Los Angeles", "Downtown and central LA"),
    "323": ("la_city", "the City of Los Angeles", "Central and East LA"),
    "818": ("la_city", "the City of Los Angeles", "San Fernando Valley (also Glendale, Burbank)"),
    "747": ("la_city", "the City of Los Angeles", "San Fernando Valley (also Glendale, Burbank)"),
    "310": ("la_city", "the City of Los Angeles", "Westside and South Bay (many cities)"),
    "424": ("la_city", "the City of Los Angeles", "Westside and South Bay (many cities)"),
    "626": ("la_county", "LA County", "San Gabriel Valley (Pasadena, Altadena, many cities)"),
    "562": ("la_county", "LA County", "Southeast county and Long Beach"),
    "661": ("la_county", "LA County", "Santa Clarita and Antelope Valley"),
}

LA_CITY_NEIGHBORHOODS = [
    "Sherman Oaks", "Studio City", "Encino", "Tarzana", "Woodland Hills", "North Hollywood",
    "Van Nuys", "Reseda", "Northridge", "Granada Hills", "Chatsworth", "Canoga Park", "Winnetka",
    "West Hills", "Panorama City", "Sun Valley", "Sylmar", "Pacoima", "Tujunga", "Sunland",
    "Valley Village", "Toluca Lake", "Hollywood", "Los Feliz", "Silver Lake", "Silverlake",
    "Echo Park", "Highland Park", "Eagle Rock", "Boyle Heights", "El Sereno", "Lincoln Heights",
    "Koreatown", "Mid-City", "Westwood", "Brentwood", "Bel Air", "Pacific Palisades", "Venice",
    "Mar Vista", "Palms", "Playa del Rey", "Playa Vista", "Westchester", "San Pedro", "Wilmington",
    "Harbor City", "Watts", "South LA", "South Los Angeles", "Leimert Park", "Hyde Park",
    "Downtown", "DTLA", "Chinatown", "Little Tokyo", "Arts District", "Fairfax", "Hancock Park",
    "Atwater Village", "Glassell Park", "Cypress Park", "Mount Washington", "Porter Ranch",
    "Arleta", "Mission Hills", "North Hills", "Lake Balboa", "Valley Glen", "Shadow Hills",
    "Exposition Park", "West Adams", "Crenshaw", "Baldwin Hills", "Del Rey", "Sawtelle",
    "Century City", "Cheviot Hills", "Beverlywood", "Larchmont", "Westlake", "Pico-Union",
]

UNINCORPORATED = [
    "East Los Angeles", "East LA", "Altadena", "Marina del Rey", "Hacienda Heights",
    "Rowland Heights", "Topanga", "View Park", "Windsor Hills", "Ladera Heights", "Willowbrook",
    "Florence-Firestone", "Walnut Park", "West Athens", "Westmont", "Lennox", "Del Aire",
    "West Carson", "Valinda", "Avocado Heights", "South Whittier", "Stevenson Ranch", "Castaic",
    "Acton", "Agua Dulce", "Lake Los Angeles", "Quartz Hill", "Littlerock", "La Crescenta",
    "Kagel Canyon", "Val Verde", "South San Gabriel", "East Pasadena", "West Whittier",
    "Los Nietos", "Charter Oak", "Universal City", "Ladera", "West Rancho Dominguez",
]

COUNTY_CITIES = [
    "Agoura Hills", "Alhambra", "Arcadia", "Artesia", "Avalon", "Azusa", "Baldwin Park", "Bell",
    "Bell Gardens", "Bellflower", "Beverly Hills", "Bradbury", "Burbank", "Calabasas", "Carson",
    "Cerritos", "Claremont", "Commerce", "Compton", "Covina", "Cudahy", "Culver City",
    "Diamond Bar", "Downey", "Duarte", "El Monte", "El Segundo", "Gardena", "Glendale", "Glendora",
    "Hawaiian Gardens", "Hawthorne", "Hermosa Beach", "Hidden Hills", "Huntington Park",
    "City of Industry", "Inglewood", "Irwindale", "La Canada Flintridge", "La Cañada Flintridge",
    "La Habra Heights", "La Mirada", "La Puente", "La Verne", "Lakewood", "Lancaster", "Lawndale",
    "Lomita", "Long Beach", "Lynwood", "Malibu", "Manhattan Beach", "Maywood", "Monrovia",
    "Montebello", "Monterey Park", "Norwalk", "Palmdale", "Palos Verdes Estates", "Paramount",
    "Pasadena", "Pico Rivera", "Pomona", "Rancho Palos Verdes", "Redondo Beach", "Rolling Hills",
    "Rolling Hills Estates", "Rosemead", "San Dimas", "San Fernando", "San Gabriel", "San Marino",
    "Santa Clarita", "Santa Fe Springs", "Santa Monica", "Sierra Madre", "Signal Hill",
    "South El Monte", "South Gate", "South Pasadena", "Temple City", "Torrance", "Vernon",
    "Walnut", "West Covina", "West Hollywood", "Westlake Village", "Whittier",
]

# A place name followed by one of these is a street ("Glendale Blvd" is in LA).
STREET_SUFFIX = r"(blvd|boulevard|ave|avenue|st|street|fwy|freeway|dr|drive|rd|road|way|pl|place|ln|lane|ct|canyon|pkwy|parkway|hwy|highway)\b"


# Cities each area code commonly covers, used to narrow the board before the
# caller confirms. City of LA and the county are always candidates.
AREA_CITIES = {
    "213": ["Vernon", "Huntington Park", "Maywood"],
    "323": ["West Hollywood", "Beverly Hills", "Inglewood", "Huntington Park", "Bell", "Commerce", "Montebello", "Vernon"],
    "818": ["Glendale", "Burbank", "San Fernando", "Calabasas", "Hidden Hills", "La Canada Flintridge"],
    "747": ["Glendale", "Burbank", "San Fernando", "Calabasas"],
    "310": ["Santa Monica", "Culver City", "Beverly Hills", "Inglewood", "Torrance", "Redondo Beach", "Manhattan Beach",
            "Hermosa Beach", "El Segundo", "Hawthorne", "Gardena", "Carson", "Compton", "Malibu", "West Hollywood"],
    "424": ["Santa Monica", "Culver City", "Beverly Hills", "Inglewood", "Torrance", "Redondo Beach", "Manhattan Beach"],
    "626": ["Pasadena", "Alhambra", "Arcadia", "Monrovia", "El Monte", "West Covina", "San Gabriel", "South Pasadena",
            "Monterey Park", "Covina", "Azusa", "Glendora", "Temple City", "Rosemead", "Sierra Madre"],
    "562": ["Long Beach", "Downey", "Norwalk", "Whittier", "Lakewood", "Bellflower", "Cerritos", "Pico Rivera",
            "Signal Hill", "Paramount", "La Mirada", "Santa Fe Springs"],
    "661": ["Santa Clarita", "Lancaster", "Palmdale"],
}


def guess_from_number(number: str | None) -> dict[str, Any]:
    digits = re.sub(r"\D", "", number or "")
    code = digits[1:4] if len(digits) == 11 and digits.startswith("1") else digits[:3]
    if code in AREA_CODES:
        key, name, region = AREA_CODES[code]
        candidates = ["la_city", "la_county"] + [city_key(c) for c in AREA_CITIES.get(code, [])]
        return {"area_code": code, "key": key, "name": name, "region": region, "candidates": candidates}
    return {"area_code": code or None, "key": None, "name": None, "region": None, "candidates": []}


def city_key(name: str) -> str:
    return "city:" + slug(name.replace("City of ", ""))


def _find(names: list[str], loc: str) -> str | None:
    for name in sorted(names, key=len, reverse=True):
        for m in re.finditer(rf"\b{re.escape(name.lower())}\b", loc):
            if not re.match(rf"\s+{STREET_SUFFIX}", loc[m.end():]):
                return name
    return None


def known_place(location: str) -> dict[str, Any] | None:
    loc = location.lower()
    if name := _find(UNINCORPORATED, loc):
        return {"key": "la_county", "name": f"{name} (unincorporated LA County)", "place": name}
    learned = [j["name"].replace("City of ", "") for k, j in rules.jurisdictions.items() if k.startswith("city:")]
    if name := _find(COUNTY_CITIES + learned, loc):
        return {"key": city_key(name), "name": f"City of {name}", "place": name}
    if name := _find(LA_CITY_NEIGHBORHOODS, loc):
        return {"key": "la_city", "name": f"{name} (City of Los Angeles)", "place": name}
    if _find(["Los Angeles", "LA"], loc):
        return {"key": "la_city", "name": "City of Los Angeles", "place": "Los Angeles"}
    return _fuzzy_place(loc, learned)


def _fuzzy_place(loc: str, learned: list[str]) -> dict[str, Any] | None:
    """Speech recognition mangles place names ("Landora" for Glendora). Snap
    single words and word pairs to the closest known place, if close enough."""
    words = re.findall(r"[a-z']+", loc)
    grams = words + [" ".join(words[i:i + 2]) for i in range(len(words) - 1)]
    tables = [(UNINCORPORATED, "la_county"), (COUNTY_CITIES + learned, "city"), (LA_CITY_NEIGHBORHOODS, "la_city")]
    best: tuple[float, str, str] | None = None
    for names, kind in tables:
        lowered = {n.lower(): n for n in names if len(n) > 4}
        for g in grams:
            if len(g) < 5:
                continue
            for m in difflib.get_close_matches(g, lowered, n=1, cutoff=0.8):
                score = difflib.SequenceMatcher(None, g, m).ratio()
                if not best or score > best[0]:
                    best = (score, lowered[m], kind)
    if not best:
        return None
    _, name, kind = best
    if kind == "la_county":
        return {"key": "la_county", "name": f"{name} (unincorporated LA County)", "place": name, "fuzzy": True}
    if kind == "city":
        return {"key": city_key(name), "name": f"City of {name}", "place": name, "fuzzy": True}
    return {"key": "la_city", "name": f"{name} (City of Los Angeles)", "place": name, "fuzzy": True}


def resolve(location: str, guess: dict[str, Any], on_progress=None) -> dict[str, Any]:
    """Return {key, name, kind, method, sources}. kind: la_city | la_county | other_city | outside."""
    def progress(state: str, **kw: Any) -> None:
        if on_progress:
            on_progress({"state": state, **kw})

    loc = (location or "").strip()
    affirm = re.fullmatch(r"(yes|yeah|yep|correct|that's right|right|uh huh|mm-?hmm)[.!]?", loc.lower())
    if (not loc or affirm) and guess.get("key"):
        return {**_kind(guess["key"]), "key": guess["key"], "name": guess["name"],
                "method": "area code, confirmed", "sources": []}

    hit = known_place(loc)
    if hit:
        cached = hit["key"] in rules.jurisdictions
        method = "cached" if cached or hit["key"] in ("la_city", "la_county") else "known city list"
        if hit.get("fuzzy"):
            method += f" (heard \"{loc}\", closest match {hit['place']})"
        return {**_kind(hit["key"]), "key": hit["key"], "name": hit["name"], "method": method, "sources": []}

    from . import websearch
    from .llm import complete_json
    progress("searching", query=loc)
    results = websearch.search(
        f"Which city is {loc} in? Is it inside the City of Los Angeles, an unincorporated area of "
        f"Los Angeles County, a separately incorporated city in LA County, or outside LA County?",
        [f"{loc} California city or unincorporated", f"{loc} Los Angeles County jurisdiction"],
    )
    progress("reading", sources=[r["url"] for r in results])
    evidence = "\n".join(f"[{r['title']}] {r['text'][:600]}" for r in results) or "(no results)"
    schema = {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["la_city", "la_county_unincorporated", "other_city", "outside_county", "unknown"]},
        "city_name": {"type": "string", "description": "The city it's in, for other_city or outside_county (e.g. San Francisco)"},
        "explanation": {"type": "string"}}, "required": ["kind", "city_name", "explanation"]}
    try:
        out, _ = complete_json(
            f"A caller reported a problem at: {loc}\nUse the evidence to decide its jurisdiction.\n\n{evidence}",
            schema)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Place extraction failed: %s", exc)
        out = {"kind": "unknown", "city_name": "", "explanation": str(exc)}
    kind = out.get("kind")
    sources = [r["url"] for r in results]
    if kind == "la_city":
        res = {"key": "la_city", "name": f"{loc} (City of Los Angeles)"}
    elif kind == "la_county_unincorporated":
        res = {"key": "la_county", "name": f"{loc} (unincorporated LA County)"}
    elif kind == "other_city" and out.get("city_name"):
        name = out["city_name"].replace("City of ", "").strip()
        res = {"key": city_key(name), "name": f"City of {name}"}
    elif kind == "outside_county" and out.get("city_name"):
        # Outside LA County is still just a city whose offices we look up.
        name = out["city_name"].replace("City of ", "").strip()
        res = {"key": city_key(name), "name": f"City of {name}"}
    elif kind == "outside_county":
        res = {"key": None, "name": loc, "kind": "outside"}
    else:
        fallback = guess.get("key") or "la_city"
        res = {"key": fallback, "name": guess.get("name") or "City of Los Angeles"}
    res = {**_kind(res["key"]), **res, "method": "web search", "sources": sources,
           "explanation": out.get("explanation", "")}
    progress("done", result=res)
    return res


def _kind(key: str | None) -> dict[str, str]:
    if key == "la_city":
        return {"kind": "la_city"}
    if key == "la_county":
        return {"kind": "la_county"}
    if key and key.startswith("city:"):
        return {"kind": "other_city"}
    return {"kind": "outside"}
