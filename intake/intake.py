"""① Intake (owner: P1). Raw chat text → TripRequest in MongoDB → ⚡ ready.

SubQ Snap answers fixed questions (intent, cities, target booking, prefs).
Dates and numbers come from a parser, because Snap can't write text.
If SubQ enables subq-1.4-small for us, swap in full JSON extraction here.
"""
import re
from datetime import date

from common import config
from common.contracts import Signal, TripRequest
from common.db import ledger_col, requests_col
from common.metrics import estimate_tokens, track
from intake import subq_client

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
KNOWN_CITIES = ["Tokyo", "Osaka", "Kyoto"]


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


def _mock_answers(text: str, bookings: list[str]) -> dict:
    t = text.lower()
    intent = ("cancel" if "cancel" in t else "extend" if ("extend" in t or "more night" in t)
              else "reschedule" if ("move" in t or "reschedule" in t) else "book")
    cities = [c for c in KNOWN_CITIES if c.lower() in t] or ["Tokyo"]
    return {"intent": {"choice": intent, "confidence": 0.9},
            "cities": cities,
            "target_booking": {"choice": bookings[0] if bookings else "none", "confidence": 0.6},
            "pace": {"choice": "relaxed", "confidence": 0.7},
            "dining_style": {"choice": "local", "confidence": 0.7}}


def _subq_answers(text: str, bookings: list[str]) -> tuple[dict, int]:
    questions = {
        "intent": subq_client.choice_q(
            "What does the traveller want to do with their trip?",
            {"book": "plan and book a new trip", "cancel": "cancel a booking or the whole trip",
             "reschedule": "move dates of an existing booking", "extend": "add nights or days"}),
        "pace": subq_client.choice_q("Preferred trip pace?", {"relaxed": None, "packed": None}),
        "dining_style": subq_client.choice_q("Preferred dining?", {"local": None, "fine": None, "budget": None}),
    }
    for c in KNOWN_CITIES:
        questions[f"city_{c.lower()}"] = subq_client.yes_no_q(f"Does the traveller plan to stay in {c}?")
    if bookings:
        questions["target_booking"] = subq_client.choice_q(
            "Which existing booking is the message about? 'none' if it is about none.",
            {**{b: None for b in bookings}, "none": None})
    state = f"Traveller message:\n{text}\n\nExisting bookings: {bookings or 'none'}"
    res = subq_client.decide(state, questions)
    a = res["answers"]
    cities = [c for c in KNOWN_CITIES if a.get(f"city_{c.lower()}", {}).get("yes", 0) >= 0.5]
    a["cities"] = cities
    return a, subq_client.input_tokens(res["usage"], state)


def run_intake(trip_id: str, raw_text: str, default_budget: float = 3500.0) -> Signal:
    bookings = [b["id"] for b in ledger_col().find({"trip_id": trip_id, "type": "booking", "status": "active"})]
    model = "mock-subq" if config.USE_MOCK_SUBQ else config.SUBQ_MODEL
    with track(trip_id, "intake", model) as m:
        if config.USE_MOCK_SUBQ:
            a = _mock_answers(raw_text, bookings)
            m.tokens(estimate_tokens(raw_text))
        else:
            a, toks = _subq_answers(raw_text, bookings)
            m.tokens(toks)

    dates = parse_dates(raw_text) or ("2026-11-15", "2026-11-22")
    target = (a.get("target_booking") or {}).get("choice")
    req = TripRequest(
        trip_id=trip_id, raw_text=raw_text,
        intent=(a["intent"]["choice"]),
        cities=a.get("cities") or ["Tokyo"],
        start=dates[0], end=dates[1],
        budget_usd=parse_budget(raw_text) or default_budget,
        events=[],  # filled below from the events collection
        prefs={k: a[k]["choice"] for k in ("pace", "dining_style") if k in a},
        target_booking=None if target in (None, "none") else target,
        intake_confidence={k: v.get("confidence") for k, v in a.items() if isinstance(v, dict) and "confidence" in v},
    )
    from common.db import get_db
    req.events = [e["id"] for e in get_db()["events"].find(
        {"city": {"$in": req.cities}, "date": {"$gte": req.start, "$lte": req.end}})]

    from common.db import plans_col
    last = plans_col().find_one({"trip_id": trip_id}, sort=[("version", -1)])
    req.first_version = (last["version"] + 1) if last else 1   # never overwrite earlier plans
    requests_col().replace_one({"trip_id": trip_id}, req.model_dump(), upsert=True)
    return Signal(type="ready", trip_id=trip_id, version=req.first_version)
