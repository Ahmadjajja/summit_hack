"""① Intake (owner: P1). Raw chat text → TripRequest in MongoDB → ⚡ ready.

SubQ Snap answers fixed questions (intent, cities, target booking, prefs).
Dates and numbers come from a parser, because Snap can't write text.
If SubQ enables subq-1.4-small for us, swap in full JSON extraction here.

Follow-up messages on an existing trip are MERGED onto the stored request:
only what the new message states changes. Change intents (cancel /
reschedule / extend) never re-ask preferences or re-derive cities; they keep
the original trip's values so downstream checks run against the real trip.
"""
import re
from datetime import date, datetime, timezone

from common import config
from common.contracts import Prefs, Signal, TripRequest
from common.db import candidates_col, clean, get_db, ledger_col, requests_col
from common.metrics import estimate_tokens, track
from intake import subq_client

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
FALLBACK_CITIES = ["Tokyo", "Osaka", "Kyoto"]  # only used when nothing is seeded yet
DEFAULT_DATES = ("2026-11-15", "2026-11-22")
CHANGE_INTENTS = ("cancel", "reschedule", "extend")
PREF_KEYS = ("pace", "dining_style")
PREF_OVERRIDE_CONFIDENCE = 0.8

_indexed_dbs: set[int] = set()


# ---------------------------------------------------------------- parsing


def parse_dates(text: str, year: int = 2026) -> tuple[str, str] | None:
    """'Nov 15-22' or 'Nov 15 to Nov 22' -> ('2026-11-15', '2026-11-22')."""
    t = text.lower()
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(\d{1,2})\s*(?:-|–|to)\s*"
                  r"(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*)?(\d{1,2})", t)
    if not m:
        return None
    m1, d1, m2, d2 = m.group(1), int(m.group(2)), m.group(3) or m.group(1), int(m.group(4))
    return (date(year, MONTHS[m1], d1).isoformat(), date(year, MONTHS[m2], d2).isoformat())


def parse_budget(text: str) -> float | None:
    m = re.search(r"\$\s?([\d,]+)", text) or re.search(r"([\d,]{3,})\s*(?:usd|dollars)", text.lower())
    return float(m.group(1).replace(",", "")) if m else None


def known_cities() -> list[str]:
    """Cities we can actually plan for = whatever is seeded in candidates/events (fix 6)."""
    found = set(candidates_col().distinct("city")) | set(get_db()["events"].distinct("city")) | set(FALLBACK_CITIES)
    found.discard(None)
    # Never shrink below the original list: an unseeded city must stay recognisable, otherwise
    # SubQ is forced to map "Kyoto" onto the nearest seeded option (seen live: Kyoto -> Osaka).
    return sorted(found)


def resolve_cities(said: list[str], subq_yes: list[str]) -> list[str]:
    """Explicitly named cities are the strongest signal; SubQ resolves negation ("skip Osaka")."""
    if said and subq_yes:
        both = [c for c in said if c in subq_yes]
        return both or said
    return said or subq_yes


def mentioned_cities(text: str, cities: list[str]) -> list[str]:
    """Cities named in the text, in the order they appear ("Tokyo then Osaka" keeps that order)."""
    t = text.lower()
    hits = [(m.start(), c) for c in cities if (m := re.search(rf"\b{re.escape(c.lower())}\b", t))]
    return [c for _, c in sorted(hits)]


def ensure_indexes() -> None:
    """One request document per trip (fix 4). Idempotent; runs once per database."""
    db = get_db()
    if id(db) in _indexed_dbs:
        return
    requests_col().create_index("trip_id", unique=True, name="uniq_trip_id")
    _indexed_dbs.add(id(db))


# ---------------------------------------------------------------- SubQ / mock answers


def _intent_questions(bookings: list[str]) -> dict:
    qs = {
        "intent": subq_client.choice_q(
            "What does the traveller want to do with their trip?",
            {"book": "plan and book a new trip, or adjust the trip being planned",
             "cancel": "cancel a booking or the whole trip",
             "reschedule": "move dates of an existing booking", "extend": "add nights or days"}),
    }
    if bookings:
        qs["target_booking"] = subq_client.choice_q(
            "Which existing booking is the message about? 'none' if it is about none.",
            {**{b: None for b in bookings}, "none": None})
    return qs


def _plan_questions(cities: list[str]) -> dict:
    qs = {
        "pace": subq_client.choice_q("Preferred trip pace?", {"relaxed": None, "packed": None}),
        "dining_style": subq_client.choice_q("Preferred dining?", {"local": None, "fine": None, "budget": None}),
    }
    for c in cities:
        qs[f"city_{c.lower()}"] = subq_client.yes_no_q(f"Does the traveller plan to stay in {c}?")
    return qs


def _ask(text: str, bookings: list[str], questions: dict) -> tuple[dict, int]:
    state = f"Traveller message:\n{text}\n\nExisting bookings: {bookings or 'none'}"
    res = subq_client.decide(state, questions)
    return res["answers"], subq_client.input_tokens(res["usage"], state)


def _mock(text: str, bookings: list[str], questions: dict) -> tuple[dict, int]:
    t = text.lower()
    a: dict = {}
    if "intent" in questions:
        intent = ("cancel" if "cancel" in t else "extend" if ("extend" in t or "more night" in t)
                  else "reschedule" if ("move" in t or "reschedule" in t) else "book")
        a["intent"] = {"choice": intent, "confidence": 0.9}
    if "target_booking" in questions:
        a["target_booking"] = {"choice": bookings[0] if bookings else "none", "confidence": 0.6}
    if "pace" in questions:
        a["pace"] = {"choice": "packed" if "packed" in t else "relaxed", "confidence": 0.7}
        a["dining_style"] = {"choice": "fine" if ("michelin" in t or "fine" in t) else "local", "confidence": 0.7}
    for q in questions:
        if q.startswith("city_"):
            a[q] = {"yes": 1.0 if q[5:] in t else 0.0}
    return a, estimate_tokens(text)


# ---------------------------------------------------------------- main


def run_intake(trip_id: str, raw_text: str, default_budget: float = 3500.0) -> Signal:
    ensure_indexes()
    existing_doc = requests_col().find_one({"trip_id": trip_id})
    existing = TripRequest(**clean(existing_doc)) if existing_doc else None
    bookings = [b["id"] for b in ledger_col().find({"trip_id": trip_id, "type": "booking", "status": "active"})]
    cities_all = known_cities()
    ask = _mock if config.USE_MOCK_SUBQ else _ask
    model = "mock-subq" if config.USE_MOCK_SUBQ else config.SUBQ_MODEL

    with track(trip_id, "intake", model) as m:
        if existing is None:
            # new trip: one call with everything
            a, toks = ask(raw_text, bookings, {**_intent_questions(bookings), **_plan_questions(cities_all)})
            m.tokens(toks)
        else:
            # follow-up: decide the intent first, only ask planning questions for a plan revision (fix 3)
            a, toks = ask(raw_text, bookings, _intent_questions(bookings))
            m.tokens(toks)
            if a["intent"]["choice"] == "book":
                more, toks2 = ask(raw_text, bookings, _plan_questions(cities_all))
                a.update(more)
                m.tokens(toks2)
            m.note = f"follow-up on existing request ({a['intent']['choice']})"

    intent = a["intent"]["choice"]
    target = (a.get("target_booking") or {}).get("choice")
    said_cities = mentioned_cities(raw_text, cities_all)
    subq_cities = [c for c in cities_all if a.get(f"city_{c.lower()}", {}).get("yes", 0) >= 0.5]
    dates = parse_dates(raw_text)
    budget = parse_budget(raw_text)
    confidence = {k: v.get("confidence") for k, v in a.items() if isinstance(v, dict) and "confidence" in v}

    new_prefs = {k: a[k]["choice"] for k in PREF_KEYS if k in a}
    resolved = resolve_cities(said_cities, subq_cities)

    if existing is None:
        defaults: set[str] = set()
        cities = resolved or ["Tokyo"]  # same fallback as before, but now flagged
        if not resolved:
            defaults.add("cities")
        start, end = dates or DEFAULT_DATES
        if not dates:
            defaults |= {"start", "end"}
        budget_usd = budget or default_budget
        if not budget:
            defaults.add("budget_usd")
        prefs = Prefs(**new_prefs)
    else:
        # merge (fix 1): keep the trip's values unless this message states new ones
        defaults = set(existing.defaults_used)
        if intent in CHANGE_INTENTS:
            cities = existing.cities  # "cancel my Osaka hotel" must not drop Tokyo from the trip
            prefs = existing.prefs
        else:
            cities = resolved if said_cities else existing.cities
            if said_cities:
                defaults.discard("cities")
            # SubQ always picks *some* option; on a follow-up only trust a pref the message actually
            # states (live: stated prefs come back >0.99 confident, unstated ones 0.25-0.66).
            stated = {k: v for k, v in new_prefs.items() if (a[k].get("confidence") or 0) >= PREF_OVERRIDE_CONFIDENCE}
            prefs = existing.prefs.model_copy(update=stated)
            # keep confidence aligned with the stored values: drop scores for guesses we ignored
            confidence = {k: v for k, v in confidence.items() if k not in PREF_KEYS or k in stated}
        start, end = dates or (existing.start, existing.end)
        if dates:
            defaults -= {"start", "end"}
        budget_usd = budget or existing.budget_usd
        if budget:
            defaults.discard("budget_usd")
        confidence = {**existing.intake_confidence, **confidence}

    req = TripRequest(
        trip_id=trip_id, raw_text=raw_text, intent=intent, cities=cities, start=start, end=end,
        budget_usd=budget_usd, events=[], prefs=prefs,
        target_booking=None if target in (None, "none") else target,
        intake_confidence=confidence,
        defaults_used=sorted(defaults),
    )
    req.events = [e["id"] for e in get_db()["events"].find(
        {"city": {"$in": req.cities}, "date": {"$gte": req.start, "$lte": req.end}})]

    doc = req.model_dump()
    # Extra field outside the TripRequest contract (readers ignore unknown keys): full message history.
    history = (existing_doc or {}).get("history", [])
    doc["history"] = history + [{"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                 "intent": intent, "raw_text": raw_text}]
    requests_col().replace_one({"trip_id": trip_id}, doc, upsert=True)
    return Signal(type="ready", trip_id=trip_id)
