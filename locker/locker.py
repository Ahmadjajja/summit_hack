"""⑤ Locker room (owner: P3). Pays EXACTLY what the approved plan allows.

The card never appears in any prompt or agent message. Every payment request
is checked against the approved plan in MongoDB, never against the request:

  1. the plan is approved and its hash matches (stored hash == recomputed hash)
  2. the items belong to that plan and to the vendor asking
  3. the amount equals the plan's cost for those items
  4. total paid for this plan never exceeds what the plan pays out
     (sum of book + paid modify actions)
  5. the same vendor+plan is never charged twice (idempotent)

Two ways in:
  - `authorize_and_charge(...)`: used by the locker room uAgent (locker/agent.py)
    when a vendor agent sends a Fetch.ai `RequestPayment`.
  - `run_locker(signal)`: the in-process path used by run_local.py (pays the
    plan's whole money_delta in one go).

BOOKING_DRY_RUN=true (default): payments are mock records. Stripe is used only if
STRIPE_TEST_KEY (sk_test_...) is set AND dry run is off. Live keys are refused.

Stripe Checkout (web UI): after approval the traveller pays the plan's total on
Stripe's hosted test page (`create_checkout`); `confirm_checkout` checks with
Stripe that it was paid, for this plan and this amount. Vendor payment requests
are then allocations of that one payment. Without a checkout (terminal chat,
ASI:One agents) the locker room charges Stripe's test card directly.
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


def stripe_enabled() -> bool:
    return bool(config.STRIPE_TEST_KEY) and not config.BOOKING_DRY_RUN


def checkout_enabled() -> bool:
    """Web UI: the traveller pays on Stripe's hosted test Checkout page (Fetch.ai's
    `stripe` payment method) instead of the locker room charging a test card itself."""
    return stripe_enabled() and config.STRIPE_CHECKOUT


def _stripe():
    if not config.STRIPE_TEST_KEY.startswith("sk_test_"):
        raise LockerRefused("only Stripe TEST keys are allowed")
    import stripe
    stripe.api_key = config.STRIPE_TEST_KEY
    return stripe


def _cents(usd: float) -> int:
    return int(round(usd * 100))


def _charge(amount_usd: float, trip_id: str, version: int, vendor: str, reference: str) -> str:
    if not stripe_enabled():
        return f"pay_mock_{uuid.uuid4().hex[:10]}"
    paid = ledger_col().find_one({"type": "checkout", "trip_id": trip_id, "version": version, "status": "paid"})
    if paid:
        # The traveller already paid the plan's total on Stripe Checkout; this vendor's share is an
        # allocation of that payment. The '#vendor' suffix keeps each allocation's ledger ID unique.
        return f"{paid['payment_intent']}#{vendor}"
    stripe = _stripe()   # no checkout (terminal chat, ASI:One agents): charge Stripe's test card directly
    pi = stripe.PaymentIntent.create(
        amount=_cents(amount_usd), currency="usd", payment_method="pm_card_visa",
        confirm=True, automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
        metadata={"trip_id": trip_id, "reference": reference})
    return pi.id


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
    costs = _payable(plan)
    unknown = [i for i in item_ids if i not in costs]
    if unknown:
        raise LockerRefused(f"items not in the approved plan: {unknown}")
    wrong_vendor = [i for i in item_ids if vendor_for(i) != vendor]
    if wrong_vendor:
        raise LockerRefused(f"{vendor} vendor cannot charge for {wrong_vendor}")
    expected = round(sum(costs[i] for i in item_ids), 2)
    if abs(expected - amount_usd) > TOLERANCE_USD:
        raise LockerRefused(f"amount ${amount_usd:,.2f} does not match approved ${expected:,.2f}")
    if _paid_so_far(trip_id, version) + amount_usd > sum(costs.values()) + TOLERANCE_USD:
        raise LockerRefused("payment would exceed the approved total")

    with track(trip_id, "locker", "stripe-test" if stripe_enabled() else "mock-payment",
               version=version) as m:
        payment_id = _charge(amount_usd, trip_id, version, vendor, reference)
        m.note = f"{vendor} {item_ids} ${amount_usd:,.2f}"
    rec = {"type": "payment", "trip_id": trip_id, "version": version, "vendor": vendor,
           "items": item_ids, "reference": reference, "payment_id": payment_id,
           "amount_usd": amount_usd, "plan_hash": plan.plan_hash, "status": "committed",
           "dry_run": not stripe_enabled(),
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    ledger_col().insert_one(dict(rec))
    return rec


def _payable(plan: PlanPackage) -> dict[str, float]:
    """Items that need money OUT: books, and modifies that cost extra."""
    return {a.id: a.cost_usd for a in plan.actions if a.op in ("book", "modify") and a.cost_usd > 0}


def _stripe_pi(payment_id: str) -> str | None:
    return payment_id.split("#")[0] if payment_id and payment_id.startswith("pi_") else None


def refund_to_card(payment_ids: list[str], amount_usd: float, trip_id: str) -> str:
    """Refund to the original card. The locker room holds the payment keys, so
    refunds go through here too. Dry run: a mock refund reference.
    A booking can have several payments (book + paid extensions), and one Checkout
    payment can cover several bookings, so the refund is spread newest-first over
    the booking's payments, never more than each still has un-refunded.
    (Before: the whole amount went to the last payment, which Stripe rejects when
    that payment is smaller, e.g. cancelling a hotel after a paid extension.)"""
    ids = [p for p in dict.fromkeys(payment_ids or []) if _stripe_pi(p)]
    if not (ids and stripe_enabled()):
        return f"re_mock_{uuid.uuid4().hex[:10]}"
    stripe = _stripe()
    left, refs = _cents(amount_usd), []
    for pid in reversed(ids):
        if left <= 0:
            break
        pi_id = _stripe_pi(pid)
        # cap by what this payment (allocation) put in, minus what was already refunded from it ...
        rec = ledger_col().find_one({"type": "payment", "payment_id": pid})
        room = _cents(rec["amount_usd"]) - _cents(rec.get("refunded_usd", 0)) if rec else left
        # ... and by what Stripe can still refund on the charge
        charge = stripe.PaymentIntent.retrieve(pi_id, expand=["latest_charge"]).latest_charge
        room = min(room, (charge.amount - charge.amount_refunded) if charge else 0)
        take = min(left, room)
        if take > 0:
            refs.append(stripe.Refund.create(payment_intent=pi_id, amount=take, metadata={"trip_id": trip_id}).id)
            left -= take
            if rec:
                ledger_col().update_one({"_id": rec["_id"]}, {"$inc": {"refunded_usd": take / 100}})
    if left > 0:
        raise LockerRefused(f"refund ${amount_usd:,.2f} exceeds what these payments can still refund")
    return ",".join(refs)


def void_payment(payment_id: str, reason: str = "") -> None:
    """A vendor couldn't book after we paid it (Fetch.ai CancelPayment): give the money back."""
    rec = ledger_col().find_one({"type": "payment", "payment_id": payment_id})
    if not rec or rec.get("status") == "cancelled":
        return
    ref = refund_to_card([payment_id], rec["amount_usd"], rec["trip_id"])
    ledger_col().update_one({"_id": rec["_id"]}, {"$set": {"status": "cancelled", "void_refund": ref,
                                                           "reason": reason}})


# ------------------------------------------------------------------ Stripe Checkout (test mode)
def _item_label(item_id: str) -> str:
    from vendors.policy import candidate
    try:
        c = candidate(item_id)
    except KeyError:
        return item_id
    name = c.get("name") or c.get("airline") or f"{c.get('area', '')} hotel"
    when = f"{c['check_in']} to {c['check_out']}" if c["kind"] == "hotel" else c.get("date", "")
    return f"{c['kind'].title()}: {name} ({when})"[:120]


def _checkout_info(s, amount: float, ui_mode: str) -> dict:
    return {"session_id": s.id, "url": getattr(s, "url", None), "client_secret": getattr(s, "client_secret", None),
            "ui_mode": ui_mode, "amount_usd": amount}


def create_checkout(trip_id: str, version: int, return_url: str, ui_mode: str = "hosted") -> dict:
    """A Stripe Checkout Session (test mode) for exactly what the approved plan pays out.

    ui_mode "hosted":   Stripe's own page (web UI): returns a `url`
    ui_mode "embedded": Fetch.ai's Agent Payment Protocol style: returns a `client_secret` the
                        chat client renders as an embedded card (with the publishable key)

    Reuses this plan's open session in the same mode. An open session in the OTHER mode is
    expired first, so one plan can never be paid twice.
    Returns {session_id, url, client_secret, ui_mode, amount_usd}."""
    plan = _approved_plan(trip_id, version, None)
    costs = _payable(plan)
    if not costs:
        raise LockerRefused("no new money needed; nothing to pay")
    amount = round(sum(costs.values()), 2)
    stripe = _stripe()
    for rec in ledger_col().find({"type": "checkout", "trip_id": trip_id, "version": version, "status": "open"}):
        s = stripe.checkout.Session.retrieve(rec["session_id"])
        if s.status == "complete":
            raise LockerRefused("this plan is already paid")
        if s.status == "open" and rec.get("ui_mode", "hosted") == ui_mode:
            return _checkout_info(s, amount, ui_mode)
        if s.status == "open":
            stripe.checkout.Session.expire(s.id)
        ledger_col().update_one({"_id": rec["_id"]}, {"$set": {"status": "expired" if s.status == "open" else s.status}})
    meta = {"trip_id": trip_id, "version": str(version), "plan_hash": plan.plan_hash}
    params = dict(
        mode="payment",   # no payment_method_types: Stripe now rejects it (methods come from the Dashboard)
        line_items=[{"quantity": 1, "price_data": {"currency": "usd", "unit_amount": _cents(cost),
                                                   "product_data": {"name": _item_label(i)}}}
                    for i, cost in costs.items()],
        metadata=meta, payment_intent_data={"metadata": meta},
        expires_at=int(datetime.now(timezone.utc).timestamp()) + 30 * 60)   # Stripe's minimum
    if ui_mode == "embedded":
        # Stripe renamed "embedded" to "embedded_page"; the Fetch.ai metadata still says "embedded"
        params.update(ui_mode="embedded_page", redirect_on_completion="if_required",
                      return_url=f"{return_url}?session_id={{CHECKOUT_SESSION_ID}}")
    else:
        params.update(success_url=f"{return_url}?session_id={{CHECKOUT_SESSION_ID}}",
                      cancel_url=f"{return_url}?cancelled=1&session_id={{CHECKOUT_SESSION_ID}}")
    s = stripe.checkout.Session.create(**params)
    ledger_col().insert_one({"type": "checkout", "trip_id": trip_id, "version": version, "session_id": s.id,
                             "ui_mode": ui_mode, "url": getattr(s, "url", None), "amount_usd": amount,
                             "plan_hash": plan.plan_hash, "status": "open",
                             "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return _checkout_info(s, amount, ui_mode)


def expire_checkout(session_id: str) -> None:
    """The traveller declined to pay (Fetch.ai RejectPayment): close the session so it can't be paid later."""
    rec = ledger_col().find_one({"type": "checkout", "session_id": session_id, "status": "open"})
    if not rec:
        return
    stripe = _stripe()
    if stripe.checkout.Session.retrieve(session_id).status == "open":   # never mark a paid session expired
        stripe.checkout.Session.expire(session_id)
        ledger_col().update_one({"_id": rec["_id"]}, {"$set": {"status": "expired"}})


def confirm_checkout(session_id: str) -> dict:
    """Check with Stripe that this session was paid, for this plan and amount. Idempotent."""
    rec = ledger_col().find_one({"type": "checkout", "session_id": session_id})
    if not rec:
        raise LockerRefused("unknown checkout session")
    if rec["status"] == "paid":
        return clean(rec)
    s = _stripe().checkout.Session.retrieve(session_id)
    if s.payment_status != "paid":
        raise LockerRefused(f"checkout not paid (status {s.status}, payment {s.payment_status})")
    plan = _approved_plan(rec["trip_id"], rec["version"], rec["plan_hash"])
    # stripe-python 16: metadata is a StripeObject, which dict() no longer accepts
    meta = s.metadata.to_dict() if hasattr(s.metadata, "to_dict") else dict(s.metadata or {})
    if meta.get("trip_id") != rec["trip_id"] or meta.get("plan_hash") != plan.plan_hash:
        raise LockerRefused("checkout session belongs to a different plan")
    if s.amount_total != _cents(rec["amount_usd"]):
        raise LockerRefused(f"Stripe charged ${s.amount_total / 100:,.2f}, plan approved ${rec['amount_usd']:,.2f}")
    ledger_col().update_one({"_id": rec["_id"]}, {"$set": {"status": "paid", "payment_intent": s.payment_intent}})
    return {**clean(rec), "status": "paid", "payment_intent": s.payment_intent}


def mark_payment(payment_id: str, status: str) -> None:
    ledger_col().update_one({"type": "payment", "payment_id": payment_id}, {"$set": {"status": status}})


def run_locker(signal: Signal) -> Signal:
    """In-process path (run_local.py): pay the plan's whole money_delta at once."""
    plan = _approved_plan(signal.trip_id, signal.version, signal.plan_hash)
    costs = _payable(plan)
    if not costs:
        raise LockerRefused("no new money needed; locker room should not have been called")
    by_vendor: dict[str, list[str]] = {}
    for i in costs:
        by_vendor.setdefault(vendor_for(i), []).append(i)
    last = None
    for vendor, ids in by_vendor.items():
        last = authorize_and_charge(signal.trip_id, signal.version, signal.plan_hash, vendor, ids,
                                    round(sum(costs[i] for i in ids), 2),
                                    reference=f"{signal.trip_id}:v{signal.version}:{vendor}")
        mark_payment(last["payment_id"], "completed")
    return Signal(type="execute", trip_id=signal.trip_id, version=signal.version,
                  payment_id=last["payment_id"] if last else None)
