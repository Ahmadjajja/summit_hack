"""⑤ Locker room (owner: P3). Pays EXACTLY what the approved plan allows.

The card never appears in any prompt or agent message. Every payment request
is checked against the approved plan in MongoDB, never against the request:

  1. the plan is approved and its hash matches (stored hash == recomputed hash)
  2. the items belong to that plan and to the vendor asking
  3. the amount equals the plan's cost for those items
  4. total paid for this plan never exceeds plan.money_delta_usd
  5. the same vendor+plan is never charged twice (idempotent)

Two ways in:
  - `authorize_and_charge(...)`: used by the locker room uAgent (locker/agent.py)
    when a vendor agent sends a Fetch.ai `RequestPayment`.
  - `run_locker(signal)`: the in-process path used by run_local.py (pays the
    plan's whole money_delta in one go).

BOOKING_DRY_RUN=true (default): payments are mock records. Stripe is used only if
STRIPE_TEST_KEY (sk_test_...) is set AND dry run is off. Live keys are refused.
"""
import uuid
from datetime import datetime, timezone

from common import config
from common.contracts import PlanPackage, Signal
from common.db import clean, ledger_col, plans_col
from common.metrics import track
from gate.gate import plan_hash

TOLERANCE_USD = 0.01


class LockerRefused(RuntimeError):
    pass


def vendor_for(item_id: str) -> str:
    return {"FLT": "flight", "HTL": "hotel", "DIN": "dining"}.get(item_id.split("-")[0], "unknown")


def _approved_plan(trip_id: str, version: int, claimed_hash: str | None) -> PlanPackage:
    doc = plans_col().find_one({"trip_id": trip_id, "version": version})
    if not doc or not doc.get("approved"):
        raise LockerRefused(f"plan {trip_id} v{version} is not approved")
    plan = PlanPackage(**clean(doc))
    if not plan.plan_hash or plan_hash(plan) != plan.plan_hash:
        raise LockerRefused("plan changed after approval (hash mismatch)")
    if claimed_hash is not None and claimed_hash != plan.plan_hash:
        raise LockerRefused("payment request refers to a different plan hash")
    return plan


def _charge(amount_usd: float, trip_id: str, reference: str) -> str:
    if config.STRIPE_TEST_KEY and not config.BOOKING_DRY_RUN:
        if not config.STRIPE_TEST_KEY.startswith("sk_test_"):
            raise LockerRefused("only Stripe TEST keys are allowed")
        import stripe
        stripe.api_key = config.STRIPE_TEST_KEY
        pi = stripe.PaymentIntent.create(
            amount=int(round(amount_usd * 100)), currency="usd", payment_method="pm_card_visa",
            confirm=True, automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            metadata={"trip_id": trip_id, "reference": reference})
        return pi.id
    return f"pay_mock_{uuid.uuid4().hex[:10]}"


def _paid_so_far(trip_id: str, version: int) -> float:
    return sum(p["amount_usd"] for p in ledger_col().find(
        {"type": "payment", "trip_id": trip_id, "version": version, "status": {"$ne": "cancelled"}}))


def authorize_and_charge(trip_id: str, version: int, claimed_hash: str | None, vendor: str,
                         item_ids: list[str], amount_usd: float, reference: str) -> dict:
    """Check a vendor's payment request against the approved plan, then pay it.
    Returns the payment record. Raises LockerRefused on any mismatch."""
    existing = ledger_col().find_one({"type": "payment", "reference": reference,
                                      "status": {"$ne": "cancelled"}})
    if existing:
        return clean(existing)  # idempotent: same request, same payment

    plan = _approved_plan(trip_id, version, claimed_hash)
    costs = {a.id: a.cost_usd for a in plan.actions if a.op == "book"}
    unknown = [i for i in item_ids if i not in costs]
    if unknown:
        raise LockerRefused(f"items not in the approved plan: {unknown}")
    wrong_vendor = [i for i in item_ids if vendor_for(i) != vendor]
    if wrong_vendor:
        raise LockerRefused(f"{vendor} vendor cannot charge for {wrong_vendor}")
    expected = round(sum(costs[i] for i in item_ids), 2)
    if abs(expected - amount_usd) > TOLERANCE_USD:
        raise LockerRefused(f"amount ${amount_usd:,.2f} does not match approved ${expected:,.2f}")
    if _paid_so_far(trip_id, version) + amount_usd > plan.money_delta_usd + TOLERANCE_USD:
        raise LockerRefused("payment would exceed the approved total")

    with track(trip_id, "locker", "mock-payment" if config.BOOKING_DRY_RUN else "stripe-test",
               version=version) as m:
        payment_id = _charge(amount_usd, trip_id, reference)
        m.note = f"{vendor} {item_ids} ${amount_usd:,.2f}"
    rec = {"type": "payment", "trip_id": trip_id, "version": version, "vendor": vendor,
           "items": item_ids, "reference": reference, "payment_id": payment_id,
           "amount_usd": amount_usd, "plan_hash": plan.plan_hash, "status": "committed",
           "dry_run": config.BOOKING_DRY_RUN,
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    ledger_col().insert_one(dict(rec))
    return rec


def mark_payment(payment_id: str, status: str) -> None:
    ledger_col().update_one({"type": "payment", "payment_id": payment_id}, {"$set": {"status": status}})


def run_locker(signal: Signal) -> Signal:
    """In-process path (run_local.py): pay the plan's whole money_delta at once."""
    plan = _approved_plan(signal.trip_id, signal.version, signal.plan_hash)
    if plan.money_delta_usd <= 0:
        raise LockerRefused("no new money needed; locker room should not have been called")
    items = [a.id for a in plan.actions if a.op == "book"]
    by_vendor: dict[str, list[str]] = {}
    for i in items:
        by_vendor.setdefault(vendor_for(i), []).append(i)
    costs = {a.id: a.cost_usd for a in plan.actions}
    last = None
    for vendor, ids in by_vendor.items():
        last = authorize_and_charge(signal.trip_id, signal.version, signal.plan_hash, vendor, ids,
                                    round(sum(costs[i] for i in ids), 2),
                                    reference=f"{signal.trip_id}:v{signal.version}:{vendor}")
        mark_payment(last["payment_id"], "completed")
    return Signal(type="execute", trip_id=signal.trip_id, version=signal.version,
                  payment_id=last["payment_id"] if last else None)
