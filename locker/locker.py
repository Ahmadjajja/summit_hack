"""⑤ Locker room (owner: P3). On ⚡ pay: pay EXACTLY the approved amount.

The signal carries no amount. Read it from the approved plan in MongoDB and
check the plan hash, so a tampered message can't make it pay more.
The card never appears in any prompt or agent message.

TODO(P3): replace `_charge` with the Fetch.ai Agent Payment Protocol + Stripe
TEST mode (see innovationlab.fetch.ai → stripe-horoscope-payment-protocol).
"""
import uuid
from datetime import datetime, timezone

from common import config
from common.contracts import PlanPackage, Signal
from common.db import clean, ledger_col, plans_col
from common.metrics import track
from gate.gate import plan_hash


class LockerRefused(RuntimeError):
    pass


def _charge(amount_usd: float, trip_id: str) -> str:
    if config.STRIPE_TEST_KEY:
        if not config.STRIPE_TEST_KEY.startswith("sk_test_"):
            raise LockerRefused("Only Stripe TEST keys are allowed.")
        import stripe
        stripe.api_key = config.STRIPE_TEST_KEY
        pi = stripe.PaymentIntent.create(amount=int(round(amount_usd * 100)), currency="usd",
                                         payment_method="pm_card_visa", confirm=True,
                                         automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
                                         metadata={"trip_id": trip_id})
        return pi.id
    return f"pay_mock_{uuid.uuid4().hex[:10]}"


def run_locker(signal: Signal) -> Signal:
    doc = plans_col().find_one({"trip_id": signal.trip_id, "version": signal.version})
    if not doc or not doc.get("approved"):
        raise LockerRefused("Plan is not approved.")
    plan = PlanPackage(**clean(doc))
    if signal.plan_hash != plan.plan_hash or plan_hash(plan) != plan.plan_hash:
        raise LockerRefused("Plan hash mismatch: the plan changed after approval.")
    amount = plan.money_delta_usd
    if amount <= 0:
        raise LockerRefused("No new money needed; locker room should not have been called.")

    with track(signal.trip_id, "locker", "stripe-test" if config.STRIPE_TEST_KEY else "mock-payment",
               version=signal.version):
        payment_id = _charge(amount, signal.trip_id)
    ledger_col().insert_one({"type": "payment", "trip_id": signal.trip_id, "version": signal.version,
                             "payment_id": payment_id, "amount_usd": amount, "plan_hash": plan.plan_hash,
                             "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return Signal(type="execute", trip_id=signal.trip_id, version=signal.version, payment_id=payment_id)
