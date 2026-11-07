"""Vendor policies: refunds, date changes, extensions (owner: P3).

Single place that knows how much a cancel refunds or a change costs. Use it:
  - P1 planner: build cancel / reschedule / extend actions with the helpers below
  - P2 verifier: rule R-12 can compare a plan's refund with `refund_for(...)`
  - P3 vendors: compute the ACTUAL refund at execution time and flag mismatches

All numbers are synthetic demo policy.
"""
from datetime import date, datetime, timedelta

from common import config
from common.contracts import Action
from common.db import candidates_col, clean, ledger_col

HOTEL_CHECK_IN_HOUR = 15
FLIGHT_CHANGE_FEE_USD = 150.0
FLIGHT_CANCEL_FEE_USD = 150.0
DINING_FREE_CANCEL_HOURS = 24


def now() -> datetime:
    return datetime.fromisoformat(config.DEMO_NOW) if config.DEMO_NOW else datetime.now()


def _d(s: str) -> date:
    return date.fromisoformat(s)


def candidate(item_id: str) -> dict:
    c = candidates_col().find_one({"id": item_id})
    if not c:
        raise KeyError(f"unknown item {item_id}")
    return clean(c)


def active_booking(trip_id: str, item_id: str) -> dict:
    b = ledger_col().find_one({"type": "booking", "trip_id": trip_id, "id": item_id, "status": "active"})
    if not b:
        raise KeyError(f"no active booking {item_id} on trip {trip_id}")
    return clean(b)


def _nights(check_in: str, check_out: str) -> int:
    return (_d(check_out) - _d(check_in)).days


# ------------------------------------------------------------------ refunds
def refund_for(booking: dict, as_of: datetime | None = None) -> float:
    """What the vendor refunds if this booking is cancelled at `as_of`."""
    as_of = as_of or now()
    kind = booking["kind"]
    if kind == "hotel":
        # Before check-in: free inside the window, else lose `penalty_nights`.
        # During the stay: tonight is charged, later nights are refunded.
        nightly, policy = booking["nightly_usd"], booking.get("policy", {})
        start = datetime.combine(_d(booking["check_in"]), datetime.min.time()).replace(hour=HOTEL_CHECK_IN_HOUR)
        if as_of < start:
            hours_before = (start - as_of).total_seconds() / 3600
            if hours_before >= policy.get("free_cancel_hours", 0):
                return float(booking["cost_usd"])
            return float(max(0.0, booking["cost_usd"] - policy.get("penalty_nights", 1) * nightly))
        unused = max(0, (_d(booking["check_out"]) - (as_of.date() + timedelta(days=1))).days)
        return float(unused * nightly)
    if kind == "flight":
        dep = datetime.fromisoformat(f"{booking['date']}T{booking['dep']}")
        return float(max(0.0, booking["cost_usd"] - FLIGHT_CANCEL_FEE_USD)) if as_of < dep else 0.0
    if kind == "dining":
        at = datetime.fromisoformat(f"{booking['date']}T19:00")
        return float(booking["cost_usd"]) if (at - as_of).total_seconds() / 3600 >= DINING_FREE_CANCEL_HOURS else 0.0
    return 0.0


# ------------------------------------------------------------------ action builders (for the planner)
def cancel_action(trip_id: str, booking_id: str, as_of: datetime | None = None) -> Action:
    b = active_booking(trip_id, booking_id)
    return Action(op="cancel", id=booking_id, cost_usd=-refund_for(b, as_of))


def extend_hotel_action(trip_id: str, booking_id: str, extra_nights: int) -> Action:
    b = active_booking(trip_id, booking_id)
    new_out = (_d(b["check_out"]) + timedelta(days=extra_nights)).isoformat()
    return Action(op="modify", id=booking_id, cost_usd=float(extra_nights * b["nightly_usd"]),
                  params={"new_check_in": b["check_in"], "new_check_out": new_out})


def shift_hotel_action(trip_id: str, booking_id: str, days: int) -> Action:
    """Move a stay by `days`, same hotel, same length. Date change is free; you pay
    only a price difference (none in the demo data)."""
    b = active_booking(trip_id, booking_id)
    return Action(op="modify", id=booking_id, cost_usd=0.0,
                  params={"new_check_in": (_d(b["check_in"]) + timedelta(days=days)).isoformat(),
                          "new_check_out": (_d(b["check_out"]) + timedelta(days=days)).isoformat()})


def change_flight_action(trip_id: str, booking_id: str, new_flight_id: str) -> Action:
    """Move a flight to another flight: change fee + fare difference (never negative)."""
    b, new = active_booking(trip_id, booking_id), candidate(new_flight_id)
    extra = FLIGHT_CHANGE_FEE_USD + max(0.0, new["price_usd"] - b["cost_usd"])
    return Action(op="modify", id=booking_id, cost_usd=float(extra),
                  params={"new_date": new["date"], "new_flight": new_flight_id, "new_dep": new["dep"],
                          "new_price_usd": new["price_usd"]})


def gross_payable(actions: list[Action]) -> float:
    """Money that must go OUT through the locker room (book + paid modifies)."""
    return round(sum(a.cost_usd for a in actions if a.op in ("book", "modify") and a.cost_usd > 0), 2)


def net_delta(actions: list[Action]) -> float:
    """Shown to the user: out minus expected refunds."""
    return round(sum(a.cost_usd for a in actions), 2)
