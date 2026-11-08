"""② Planner for mid-trip changes: cancel / reschedule / extend.

Called by planner.run_planner when the request's intent isn't "book". Reads the
trip's ACTIVE bookings from the ledger and builds actions with the vendor-policy
helpers in vendors/policy.py, so every refund and price follows vendor policy.
SubQ Snap picks the replacement flight when there's a choice (mock: cheapest).

  cancel      the target booking, or every booking for "cancel the whole trip"
  extend      +N nights on the target hotel; if it's the last stay, move the
              return flight to the new check-out date
  reschedule  shift the target hotel by ±N days (same for the return flight if
              it's the last stay), or move a target flight by ±N days

If the target can't be worked out the plan has no actions; the verifier's R-15
blocks it and the gate asks the traveller to say which booking they mean.
"""
import re
from datetime import date, timedelta

from common import config
from common.contracts import Action, PlanPackage, Signal, TripRequest
from common.db import candidates_col, clean, ledger_col, plans_col
from common.metrics import track
from intake import subq_client
from scout.scout import find_flights
from vendors import policy

WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "couple of": 2}
WHOLE_TRIP = re.compile(r"\b(whole|entire|full)\s+trip\b|\beverything\b|\ball\s+(my\s+)?bookings\b", re.I)


def _number(text: str, unit: str, default: int) -> int:
    m = re.search(rf"(\d+|a|an|one|two|three|four|five|couple of)\s+(more\s+|extra\s+)?{unit}s?\b", text, re.I)
    if not m:
        return default
    w = m.group(1).lower()
    return int(w) if w.isdigit() else WORDS.get(w, default)


def _active(trip_id: str) -> dict[str, dict]:
    return {b["id"]: clean(b) for b in ledger_col().find({"trip_id": trip_id, "type": "booking", "status": "active"})}


def _pick_flight(trip_id: str, version: int, options: list[dict], why: str, meter) -> dict | None:
    if not options:
        return None
    if config.USE_MOCK_SUBQ or len(options) == 1:
        return min(options, key=lambda c: c["price_usd"])
    q = {"flight": subq_client.choice_q(
        f"Pick the best replacement flight: {why}. Prefer a sensible departure time and good value.",
        {c["id"]: f"{c['airline']} {c['date']} {c['dep']} · ${c['price_usd']}" for c in options})}
    res = subq_client.decide(f"Trip {trip_id}: {why}", q)
    meter.tokens(subq_client.input_tokens(res["usage"], why))
    choice = res["answers"]["flight"]["choice"]
    return next((c for c in options if c["id"] == choice), options[0])


def _return_flight(active: dict[str, dict]) -> dict | None:
    flights = sorted((b for b in active.values() if b["kind"] == "flight"), key=lambda b: (b["date"], b["dep"]))
    return flights[-1] if len(flights) >= 2 else None


def _move_return_flight(trip_id, version, active, hotel_city, new_date, meter) -> Action | None:
    ret = _return_flight(active)
    if not ret or ret["date"] == new_date:
        return None
    options = [clean(c) for c in candidates_col().find({"kind": "flight", "from": hotel_city, "date": new_date})]
    if not options:   # e.g. a trip planned from live web results: search for the new return flight
        options = find_flights(trip_id, hotel_city, ret["to"], new_date)
    chosen = _pick_flight(trip_id, version, options, f"fly home from {hotel_city} on {new_date}", meter)
    return policy.change_flight_action(trip_id, ret["id"], chosen["id"]) if chosen else None


def build_actions(req: TripRequest, version: int, meter) -> tuple[list[Action], str]:
    trip_id, text = req.trip_id, req.raw_text
    active = _active(trip_id)
    if req.intent == "cancel" and WHOLE_TRIP.search(text):
        return [policy.cancel_action(trip_id, b) for b in active], "cancel the whole trip"
    target = active.get(req.target_booking or "")
    if target is None:
        return [], "couldn't tell which booking the message is about"

    if req.intent == "cancel":
        return [policy.cancel_action(trip_id, target["id"])], f"cancel {target['id']}"

    hotels = sorted((b for b in active.values() if b["kind"] == "hotel"), key=lambda b: b["check_out"])
    last_hotel = hotels[-1] if hotels else None

    if req.intent == "extend":
        hotel = target if target["kind"] == "hotel" else last_hotel
        if hotel is None:
            return [], "no hotel to extend"
        n = _number(text, "night", default=_number(text, "day", default=1))
        acts = [policy.extend_hotel_action(trip_id, hotel["id"], n)]
        if last_hotel and hotel["id"] == last_hotel["id"]:
            new_out = (date.fromisoformat(hotel["check_out"]) + timedelta(days=n)).isoformat()
            flight = _move_return_flight(trip_id, version, active, hotel["city"], new_out, meter)
            if flight:
                acts.append(flight)
        return acts, f"extend {hotel['id']} by {n} night(s)"

    # reschedule
    n = _number(text, "day", default=1) * (-1 if re.search(r"\bearlier\b|\bsooner\b", text, re.I) else 1)
    if target["kind"] == "hotel":
        acts = [policy.shift_hotel_action(trip_id, target["id"], n)]
        if last_hotel and target["id"] == last_hotel["id"]:
            new_out = (date.fromisoformat(target["check_out"]) + timedelta(days=n)).isoformat()
            flight = _move_return_flight(trip_id, version, active, target["city"], new_out, meter)
            if flight:
                acts.append(flight)
        return acts, f"move {target['id']} by {n} day(s)"
    if target["kind"] == "flight":
        new_date = (date.fromisoformat(target["date"]) + timedelta(days=n)).isoformat()
        options = [clean(c) for c in candidates_col().find({"kind": "flight", "from": target["from"],
                                                           "to": target["to"], "date": new_date})]
        if not options:
            options = find_flights(trip_id, target["from"], target["to"], new_date, target.get("slot", "flight_ret"))
        chosen = _pick_flight(trip_id, version, options, f"move {target['id']} to {new_date}", meter)
        if not chosen:
            return [], f"no flight from {target['from']} on {new_date}"
        return [policy.change_flight_action(trip_id, target["id"], chosen["id"])], f"move {target['id']}"
    return [], f"{target['kind']} bookings can't be rescheduled; cancel and book instead"


def run_change_planner(signal: Signal, req: TripRequest) -> Signal:
    model = "mock-subq" if config.USE_MOCK_SUBQ else config.SUBQ_MODEL
    with track(req.trip_id, "planner", model, loop=signal.version, version=signal.version) as m:
        actions, note = build_actions(req, signal.version, m)
        m.note = note
    plan = PlanPackage(trip_id=req.trip_id, version=signal.version, intent=req.intent, slots=[], actions=actions,
                       total_usd=policy.net_delta(actions), money_delta_usd=policy.net_delta(actions))
    plans_col().replace_one({"trip_id": req.trip_id, "version": signal.version}, plan.model_dump(), upsert=True)
    return Signal(type="check", trip_id=req.trip_id, version=signal.version)
