"""⑥ Execute (owner: P3). On ⚡ execute: carry out the approved plan's actions
through the vendor agents (flight / hotel / dining), write bookings to the ledger.

Today these are plain functions against mock inventory. TODO(P3): run each
vendor as a uAgent on Agentverse (see vendors/agents.py) and send the actions
as agent messages. Keep the ledger writes identical.
"""
import uuid
from datetime import datetime, timezone

from common.contracts import PlanPackage, Signal
from common.db import clean, ledger_col, plans_col
from common.metrics import track

VENDOR_BY_PREFIX = {"FLT": "flight-agent", "HTL": "hotel-agent", "DIN": "dining-agent"}


def _vendor_for(item_id: str) -> str:
    return VENDOR_BY_PREFIX.get(item_id.split("-")[0], "unknown-agent")


def book(trip_id: str, item_id: str, cost: float) -> dict:
    rec = {"type": "booking", "trip_id": trip_id, "id": item_id, "vendor": _vendor_for(item_id),
           "confirmation": f"CF-{uuid.uuid4().hex[:6].upper()}", "cost_usd": cost, "status": "active",
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    ledger_col().insert_one(dict(rec))
    return rec


def cancel(trip_id: str, item_id: str, refund_usd: float) -> dict:
    ledger_col().update_one({"type": "booking", "trip_id": trip_id, "id": item_id, "status": "active"},
                            {"$set": {"status": "cancelled"}})
    rec = {"type": "refund", "trip_id": trip_id, "id": item_id, "amount_usd": refund_usd,
           "to": "original card", "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    ledger_col().insert_one(dict(rec))
    return rec


def run_execute(signal: Signal) -> str:
    doc = plans_col().find_one({"trip_id": signal.trip_id, "version": signal.version})
    if not doc or not doc.get("approved"):
        raise RuntimeError("Refusing to execute an unapproved plan.")
    plan = PlanPackage(**clean(doc))
    if plan.money_delta_usd > 0 and not signal.payment_id:
        raise RuntimeError("New money needed but no payment_id: locker room must run first.")
    lines = [f"✅ Done for trip {plan.trip_id} (plan v{plan.version})"]
    with track(signal.trip_id, "execute", "fetch-vendor-agents", version=signal.version):
        for a in plan.actions:
            if a.op == "book":
                r = book(plan.trip_id, a.id, a.cost_usd)
                lines.append(f"- booked {a.id} via {r['vendor']} · conf {r['confirmation']}")
            elif a.op == "cancel":
                r = cancel(plan.trip_id, a.id, -a.cost_usd)
                lines.append(f"- cancelled {a.id} · refund ${r['amount_usd']:,.0f} to original card")
            # TODO(P3): "modify" for reschedule/extend (book new first, then cancel old)
    if signal.payment_id:
        lines.append(f"Paid ${plan.money_delta_usd:,.0f} (payment {signal.payment_id}).")
    return "\n".join(lines)
