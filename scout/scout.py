"""Live candidates from the web (Fetch.ai ASI:One web search).

The planner and verifier only ever choose between candidates stored in MongoDB,
and the locker room only pays the approved candidate's stored price. So live web
results are NOT handed to the planner as text: they are validated, given an ID
and a `source_url`, and saved as normal candidates. From there the whole
pipeline (SubQ picks, Gemini + rules verify, human approves, locker pays exactly
that price) works the same as for the seeded Japan demo.

    extract_destination(text)  cities + origin for a place we have no data for
    ensure_candidates(trip_id) search only the slots that have no options yet

A slot that already has options (e.g. the seeded Japan trip) is never searched.
"""
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from common import config
from common.db import candidates_col, clean, requests_col
from common.metrics import estimate_tokens, track
from scout import asi_search
from scout.asi_search import SearchError

MONTH_WORDS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)")
DINING_RE = re.compile(r"\b(dinner|dinners|restaurant|restaurants|food|eat|dining|omakase)\b", re.I)
DEFAULT_POLICY = {"free_cancel_hours": 48, "penalty_nights": 1}
MAX_OPTIONS = 4


def slug(city: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", city.lower()).strip("_")


def _id(prefix: str, *parts) -> str:
    return f"{prefix}-W{hashlib.sha1('|'.join(map(str, parts)).lower().encode()).hexdigest()[:6].upper()}"


def split_nights(cities: list[str], start: str, end: str) -> list[tuple[str, str, str]]:
    """Spread the nights over the cities in order, extra nights to the earlier ones
    (7 nights, Tokyo then Osaka → Tokyo 4, Osaka 3, as in the seeded demo)."""
    d0, total = date.fromisoformat(start), (date.fromisoformat(end) - date.fromisoformat(start)).days
    cities = [c for c in cities if c][:max(1, total)] or []
    out, d = [], d0
    for i, c in enumerate(cities):
        n = total // len(cities) + (1 if i < total % len(cities) else 0)
        out.append((c, d.isoformat(), (d + timedelta(days=n)).isoformat()))
        d += timedelta(days=n)
    return out


# ------------------------------------------------------------------ destination
def parse_origin(text: str) -> str | None:
    m = re.search(r"\bfrom\s+([A-Z]{3}\b|[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})", text or "")
    if not m or re.fullmatch(MONTH_WORDS, m.group(1).lower()):
        return None
    return m.group(1)


def _mock_destination(text: str) -> list[str]:
    found = []
    for m in re.finditer(r"\b(?:to|in|visit|visiting|then|and)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2})", text):
        words = [w for w in m.group(1).split() if not re.fullmatch(MONTH_WORDS, w.lower())]
        city = " ".join(words)
        if city and city.upper() != city and city not in found:
            found.append(city)
    return found


def extract_destination(text: str, trip_id: str | None = None) -> dict:
    """{"cities": [...in travel order], "origin": str|None}. Used only when intake
    recognises none of the cities we already have data for."""
    origin = parse_origin(text)
    if config.USE_MOCK_ASI or not config.LIVE_SEARCH:
        return {"cities": _mock_destination(text), "origin": origin}
    prompt = ("Extract the travel destination from this trip request. Reply ONLY with JSON "
              '{"cities": [city names in the order the traveller visits them, proper English names], '
              '"origin": "departure city or airport code, or null"}. If only a country or region is named, '
              "use its main city for visitors. Do not include the origin in cities.\n\n"
              f"Trip request: {text}")
    with track(trip_id or "-", "search", config.ASI_ONE_MODEL) as m:
        try:
            data, info = asi_search.ask_json(prompt)
            m.tokens(info["input_tokens"], info["output_tokens"])
        except SearchError as e:
            m.note = f"destination extraction failed: {e}"
            return {"cities": _mock_destination(text), "origin": origin}
        cities = [str(c).strip() for c in (data or {}).get("cities") or [] if str(c).strip()]
        m.note = f"destination {cities} from message"
    return {"cities": cities, "origin": origin or ((data or {}).get("origin") or None)}


# ------------------------------------------------------------------ validation
def _num(v) -> float | None:
    try:
        x = float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def _hhmm(v) -> str | None:
    m = _HHMM.match(str(v or "").strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def _url(v) -> str | None:
    s = str(v or "").strip()
    return s if s.startswith(("http://", "https://")) else None


def _meta(source: str, url) -> dict:
    return {"source": source, "source_url": _url(url),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def flight_candidates(rows, slot: str, day: str, frm: str, to: str, source: str) -> list[dict]:
    out = []
    for r in rows or []:
        price, dep, airline = _num(r.get("price_usd")), _hhmm(r.get("dep")), str(r.get("airline") or "").strip()
        if not (price and dep and airline):
            continue
        h = int(dep[:2])
        out.append({"id": _id("FLT", slot, day, frm, to, airline, dep), "kind": "flight", "slot": slot,
                    "date": day, "from": frm, "to": to, "dep": dep, "airline": airline,
                    "flight_no": str(r.get("flight_no") or "").strip() or None, "price_usd": round(price, 2),
                    "convenience": round(max(0.1, 1 - abs(h - 11) / 12), 2), **_meta(source, r.get("url"))})
    return out[:MAX_OPTIONS]


def hotel_candidates(rows, city: str, check_in: str, check_out: str, source: str) -> list[dict]:
    out = []
    for r in rows or []:
        nightly, name = _num(r.get("nightly_usd")), str(r.get("name") or "").strip()
        if not (nightly and name):
            continue
        rating = min(5.0, _num(r.get("rating")) or 0.0)
        free_h = _num(r.get("free_cancel_hours"))
        policy = {**DEFAULT_POLICY, **({"free_cancel_hours": int(free_h)} if free_h else {})}
        out.append({"id": _id("HTL", city, check_in, check_out, name), "kind": "hotel", "slot": f"hotel_{slug(city)}",
                    "city": city, "name": name, "area": str(r.get("area") or city).strip(),
                    "check_in": check_in, "check_out": check_out, "nightly_usd": round(nightly, 2),
                    "rating": round(rating, 1), "convenience": round(rating / 5, 2) if rating else 0.5,
                    "policy": policy, "policy_assumed": not free_h, **_meta(source, r.get("url"))})
    return out[:MAX_OPTIONS]


def dining_candidates(rows, city: str, day: str, source: str) -> list[dict]:
    out = []
    for r in rows or []:
        price, name = _num(r.get("price_usd")), str(r.get("name") or "").strip()
        if not (price and name):
            continue
        rating = min(5.0, _num(r.get("rating")) or 0.0)
        out.append({"id": _id("DIN", city, day, name), "kind": "dining",
                    "slot": f"dinner_{slug(city)}_{day[5:7]}{day[8:]}", "city": city, "date": day, "name": name,
                    "area": str(r.get("area") or city).strip(), "price_usd": round(price, 2),
                    "rating": round(rating, 1), "distance_mi": _num(r.get("distance_mi")) or 1.0,
                    "convenience": round(rating / 5, 2) if rating else 0.5, **_meta(source, r.get("url"))})
    return out[:MAX_OPTIONS]


# ------------------------------------------------------------------ search tasks
def _flights_prompt(origin, first, start, last, end) -> str:
    return (f"Search the web for current economy fares for a round trip: outbound {origin} → {first} on {start}, "
            f"return {last} → {origin} on {end}. Give up to 3 real options per leg (real airlines that fly the "
            "route; nonstop preferred), one-way price per person in USD as listed on airline or fare sites. "
            "If exact-date fares aren't published yet, use the typical current fare for that route. Reply ONLY with JSON: "
            '{"outbound": [{"airline": str, "flight_no": str, "dep": "HH:MM local", "price_usd": number, "url": str}], '
            '"return": [same fields]}')


def _stay_prompt(city, check_in, check_out, dinner_day) -> str:
    dinner = (f' "dinners": up to 3 well-rated restaurants in {city} for dinner on {dinner_day}, each '
              '{"name": str, "area": str, "price_usd": number (typical dinner per person), "rating": number 0-5, "url": str}.'
              if dinner_day else ' "dinners": [].')
    return (f"Search the web for real hotels in {city} for {check_in} to {check_out}. Reply ONLY with JSON: "
            f'{{"hotels": up to 3 options at different prices, each {{"name": str, "area": str (neighbourhood), '
            '"nightly_usd": number (typical current nightly rate in USD), "rating": number 0-5 (guest rating), '
            '"free_cancel_hours": number or null, "url": str},' + dinner + "}")


def _mock_flights(origin, first, start, last, end):
    legs = {"outbound": [], "return": []}
    for i, (air, dep, price) in enumerate([("Mock Air", "10:30", 420), ("Demo Airlines", "13:15", 380),
                                           ("Sample Jet", "07:00", 510)]):
        legs["outbound"].append({"airline": air, "flight_no": f"MK{100 + i}", "dep": dep, "price_usd": price,
                                 "url": "https://example.com/mock-fare"})
        legs["return"].append({"airline": air, "flight_no": f"MK{200 + i}", "dep": dep, "price_usd": price + 20,
                               "url": "https://example.com/mock-fare"})
    return legs


def _mock_stay(city, check_in, check_out, dinner_day):
    return {"hotels": [{"name": f"{city} Central Hotel", "area": "Downtown", "nightly_usd": 210, "rating": 4.5,
                        "free_cancel_hours": 48, "url": "https://example.com/mock-hotel"},
                       {"name": f"{city} Budget Inn", "area": "Downtown", "nightly_usd": 140, "rating": 4.4,
                        "free_cancel_hours": 24, "url": "https://example.com/mock-hotel"},
                       {"name": f"Grand {city}", "area": "Old Town", "nightly_usd": 320, "rating": 4.8,
                        "url": "https://example.com/mock-hotel"}],
            "dinners": [{"name": f"{city} Bistro", "area": "Downtown", "price_usd": 55, "rating": 4.6,
                         "url": "https://example.com/mock-restaurant"},
                        {"name": "Corner Kitchen", "area": "Downtown", "price_usd": 35, "rating": 4.5,
                         "url": "https://example.com/mock-restaurant"}] if dinner_day else []}


def _run(trip_id: str, what: str, prompt: str, mock_value):
    """One search call, logged to run_log as step 'search'. Returns (data, error)."""
    model = "mock-web" if config.USE_MOCK_ASI else config.ASI_ONE_MODEL
    with track(trip_id, "search", model) as m:
        if config.USE_MOCK_ASI:
            m.tokens(estimate_tokens(prompt))
            m.note = f"{what} (mock)"
            return mock_value, None
        try:
            data, info = asi_search.web_search_json(prompt)
        except SearchError as e:
            m.note = f"{what}: FAILED {e}"
            return None, str(e)
        m.tokens(info["input_tokens"], info["output_tokens"])
        m.note = f"{what}: searched {info['queries'][:4]}"
        return (data if isinstance(data, dict) else {}), None


_tried_legs: set[tuple] = set()   # a leg that found nothing isn't searched again in the next loop


def find_flights(trip_id: str, frm: str, to: str, day: str, slot: str = "flight_ret") -> list[dict]:
    """Flight options for one leg (used by changes: 'stay 1 more night' needs a return flight on the
    new date). Returns stored options; searches the web only if there are none."""
    query = {"kind": "flight", "from": frm, "to": to, "date": day}
    found = [clean(c) for c in candidates_col().find(query)]
    key = (frm.lower(), to.lower(), day)
    if found or not config.LIVE_SEARCH or key in _tried_legs:
        return found
    _tried_legs.add(key)
    prompt = (f"Search the web for current one-way economy fares {frm} → {to} on {day}. Give up to 3 real options "
              "(real airlines that fly the route; nonstop preferred), price per person in USD. If exact-date fares "
              "aren't published yet, use the typical current fare. Reply ONLY with JSON: "
              '{"flights": [{"airline": str, "flight_no": str, "dep": "HH:MM local", "price_usd": number, "url": str}]}')
    mock = {"flights": _mock_flights(frm, to, day, to, day)["outbound"]}
    data, err = _run(trip_id, f"flight {frm}→{to} {day}", prompt, mock)
    if err:
        return []
    source = "mock-web" if config.USE_MOCK_ASI else "web:asi1"
    for c in flight_candidates(data.get("flights"), slot, day, frm, to, source):
        candidates_col().replace_one({"id": c["id"]}, dict(c), upsert=True)
    return [clean(c) for c in candidates_col().find(query)]


def _has(query: dict) -> bool:
    return candidates_col().count_documents(query, limit=1) > 0


def ensure_candidates(trip_id: str) -> dict:
    """Search the web for whatever slots of this trip have no candidates yet.
    Returns {"searched": [...], "missing": [...], "errors": [...]}."""
    doc = requests_col().find_one({"trip_id": trip_id}) or {}
    report = {"searched": [], "missing": [], "errors": []}
    if doc.get("intent") != "book" or not doc.get("cities"):
        return report
    cities, start, end = doc["cities"], doc["start"], doc["end"]
    origin = doc.get("origin") or config.DEFAULT_ORIGIN
    stays = split_nights(cities, start, end)
    if not stays:
        report["missing"].append("a stay of at least one night")
        return report
    dinners = bool(DINING_RE.search(doc.get("raw_text", "")))
    source = "mock-web" if config.USE_MOCK_ASI else "web:asi1"

    tasks = []
    first, last = cities[0], cities[-1]
    need_out = not _has({"kind": "flight", "slot": "flight_out", "date": start, "to": first})
    need_ret = not _has({"kind": "flight", "slot": "flight_ret", "date": end, "from": last})
    if need_out or need_ret:
        tasks.append(("flights", f"flights {origin}⇄{first}/{last}",
                      _flights_prompt(origin, first, start, last, end), _mock_flights(origin, first, start, last, end)))
    for city, ci, co in stays:
        need_hotel = not _has({"kind": "hotel", "city": city, "check_in": ci, "check_out": co})
        day = (date.fromisoformat(ci) + timedelta(days=1)).isoformat() if dinners else None
        if day and day >= co:
            day = ci
        need_dinner = bool(day) and not _has({"kind": "dining", "city": city, "date": day})
        if need_hotel or need_dinner:
            tasks.append((("stay", city, ci, co, day if need_dinner else None, need_hotel), f"stay {city} {ci}→{co}",
                          _stay_prompt(city, ci, co, day if need_dinner else None),
                          _mock_stay(city, ci, co, day if need_dinner else None)))
    if not tasks or not config.LIVE_SEARCH:
        return report

    with ThreadPoolExecutor(max_workers=min(4, len(tasks))) as pool:
        results = list(pool.map(lambda t: _run(trip_id, t[1], t[2], t[3]), tasks))

    new: list[dict] = []
    for (key, what, _, _), (data, err) in zip(tasks, results):
        report["searched"].append(what)
        if err:
            report["errors"].append(f"{what}: {err}")
            continue
        if key == "flights":
            if need_out:
                new += flight_candidates(data.get("outbound"), "flight_out", start, origin, first, source)
            if need_ret:
                new += flight_candidates(data.get("return"), "flight_ret", end, last, origin, source)
        else:
            _, city, ci, co, day, need_hotel = key
            if need_hotel:
                new += hotel_candidates(data.get("hotels"), city, ci, co, source)
            if day:
                new += dining_candidates(data.get("dinners"), city, day, source)
    for c in new:
        candidates_col().replace_one({"id": c["id"]}, dict(c), upsert=True)

    # what is still missing after the search
    if not _has({"kind": "flight", "slot": "flight_out", "date": start, "to": first}):
        report["missing"].append(f"a flight {origin} → {first} on {start}")
    if not _has({"kind": "flight", "slot": "flight_ret", "date": end, "from": last}):
        report["missing"].append(f"a flight {last} → {origin} on {end}")
    for city, ci, co in stays:
        if not _has({"kind": "hotel", "city": city, "check_in": ci, "check_out": co}):
            report["missing"].append(f"a hotel in {city} for {ci} → {co}")
    return report

