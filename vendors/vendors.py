"""⑥ Execute (owner: P3): the vendor-side operations, shared by the in-process
path (run_local.py) and the Fetch.ai vendor agents (vendors/agents.py).

    book    candidate → new active booking
    modify  existing booking → new dates / new flight (extend, reschedule)
    cancel  existing booking → refund computed from the VENDOR's policy;
            if it differs from what the plan expected, the result says so

Order matters for changes: books and modifies first, cancels only after they
all succeed, so a traveller is never left without a room or a seat.
"""
import uuid
from datetime import datetime, timezone

from common.contracts import Action, PlanPackage, Signal
from common.db import clean, ledger_col, plans_col
from common.metrics import track
from vendors import policy

VENDOR_BY_PREFIX = {"FLT": "flight", "HTL": "hotel", "DIN": "dining"}


def vendor_for(item_id: str) -> str:
    return VENDOR_BY_PREFIX.get(item_id.split("-")[0], "unknown")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def book(trip_id: str, item_id: str, cost: float, payment_id: str | None = None) -> dict:
    c = policy.candidate(item_id)
    rec = {**{k: v for k, v in c.items() if k not in ("convenience", "slot")},
           "type": "booking", "trip_id": trip_id, "id": item_id, "vendor": vendor_for(item_id),
           "confirmation": f"CF-{uuid.uuid4().hex[:6].upper()}", "cost_usd": float(cost),
           "status": "active", "payment_ids": [payment_id] if payment_id else [], "at": _now_iso()}
    ledger_col().insert_one(dict(rec))
    return rec


def modify(trip_id: str, item_id: str, params: dict, extra_cost: float, payment_id: str | None = None) -> dict:
    b = policy.active_booking(trip_id, item_id)
    changes: dict = {}
    if b["kind"] == "hotel":
        changes = {"check_in": params["new_check_in"], "check_out": params["new_check_out"]}
        nights = policy._nights(changes["check_in"], changes["check_out"])
        changes["cost_usd"] = float(nights * b["nightly_usd"])
        desc = f"{item_id} now {changes['check_in']} → {changes['check_out']} ({nights} nights)"
    elif b["kind"] == "flight":
        new = policy.candidate(params["new_flight"])
        changes = {"date": new["date"], "dep": new["dep"], "flight_no": params["new_flight"],
                   "cost_usd": float(b["cost_usd"] + extra_cost)}
        desc = f"{item_id} moved to {params['new_flight']} on {new['date']} {new['dep']}"
    else:
        raise ValueError(f"{b['kind']} bookings can't be modified; cancel and book instead")
    update = {"$set": changes}
    if payment_id:
        update["$push"] = {"payment_ids": payment_id}
    ledger_col().update_one({"type": "booking", "trip_id": trip_id, "id": item_id, "status": "active"}, update)
    ledger_col().insert_one({"type": "change", "trip_id": trip_id, "id": item_id, "params": params,
                             "extra_usd": extra_cost, "payment_id": payment_id, "at": _now_iso()})
    return {"id": item_id, "desc": desc, "extra_usd": extra_cost}


def cancel(trip_id: str, item_id: str, expected_refund: float) -> dict:
    b = policy.active_booking(trip_id, item_id)
    actual = policy.refund_for(b)
    ledger_col().update_one({"type": "booking", "trip_id": trip_id, "id": item_id, "status": "active"},
                            {"$set": {"status": "cancelled"}})
    from locker.locker import refund_to_card
    refund_ref = refund_to_card(b.get("payment_ids", []), actual, trip_id) if actual > 0 else None
    rec = {"type": "refund", "trip_id": trip_id, "id": item_id, "amount_usd": actual,
           "expected_usd": expected_refund, "matches_plan": abs(actual - expected_refund) < 0.01,
           "to": "original card", "refund_ref": refund_ref, "at": _now_iso()}
    ledger_col().insert_one(dict(rec))
    return rec


def describe(op: str, item_id: str, r: dict) -> str:
    if op == "book":
        return f"booked {item_id} · conf {r['confirmation']}"
    if op == "modify":
        extra = f" · paid ${r['extra_usd']:,.0f}" if r["extra_usd"] > 0 else " · no extra cost"
        return f"changed {r['desc']}{extra}"
    line = f"cancelled {item_id} · refund ${r['amount_usd']:,.0f} to original card"
    if not r["matches_plan"]:
        line += f" ⚠️ plan expected ${r['expected_usd']:,.0f}"
    return line


def precheck(trip_id: str, a: Action) -> str | None:
    """Return why this action can't be done, or None. Run BEFORE asking for money."""
    try:
        if a.op == "book":
            policy.candidate(a.id)
        else:
            b = policy.active_booking(trip_id, a.id)
            if a.op == "modify" and b["kind"] == "flight":
                policy.candidate(a.params["new_flight"])
            elif a.op == "modify" and b["kind"] not in ("hotel", "flight"):
                return f"{b['kind']} bookings can't be modified"
    except (KeyError, TypeError) as e:
        return str(e).strip("'\"")
    return None


def apply(trip_id: str, a: Action, payment_id: str | None = None) -> str:
    if a.op == "book":
        return describe("book", a.id, book(trip_id, a.id, a.cost_usd, payment_id))
    if a.op == "modify":
        return describe("modify", a.id, modify(trip_id, a.id, a.params, a.cost_usd, payment_id))
    return describe("cancel", a.id, cancel(trip_id, a.id, -a.cost_usd))


def phases(actions: list[Action]) -> list[list[Action]]:
    """Books + modifies first; cancels only after those succeed."""
    first = [a for a in actions if a.op in ("book", "modify")]
    second = [a for a in actions if a.op == "cancel"]
    return [p for p in (first, second) if p]


def run_execute(signal: Signal) -> str:
    """In-process path (run_local.py). The agent path is vendors/agents.py."""
    doc = plans_col().find_one({"trip_id": signal.trip_id, "version": signal.version})
    if not doc or not doc.get("approved"):
        raise RuntimeError("Refusing to execute an unapproved plan.")
    plan = PlanPackage(**clean(doc))
    if policy.gross_payable(plan.actions) > 0 and not signal.payment_id:
        raise RuntimeError("New money needed but no payment_id: locker room must run first.")
    lines = [f"✅ Done for trip {plan.trip_id} (plan v{plan.version})"]
    problems = [f"{a.op} {a.id}: {why}" for a in plan.actions if (why := precheck(plan.trip_id, a))]
    if problems:
        return "⚠️ Nothing changed. " + "; ".join(problems)
    # each booking records ITS vendor's payment (before: every booking got the last vendor's payment ID,
    # so a hotel refund went to e.g. the $60 dinner payment and Stripe refused it)
    by_vendor = {p["vendor"]: p["payment_id"] for p in ledger_col().find(
        {"type": "payment", "trip_id": plan.trip_id, "version": plan.version, "status": {"$ne": "cancelled"}})}
    with track(signal.trip_id, "execute", "fetch-vendor-agents", version=signal.version):
        for phase in phases(plan.actions):
            for a in phase:
                pay = by_vendor.get(vendor_for(a.id), signal.payment_id) if a.cost_usd > 0 else None
                lines.append("- " + apply(plan.trip_id, a, pay))
    paid = policy.gross_payable(plan.actions)
    if paid:
        lines.append(f"Paid ${paid:,.0f} through the locker room.")
    return "\n".join(lines)
