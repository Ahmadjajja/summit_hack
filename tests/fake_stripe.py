"""A fake `stripe` module that behaves like Stripe's test mode, for offline tests.

    from tests import fake_stripe; fake_stripe.install()   # before importing locker
    fake_stripe.pay(session_id)    # what the traveller does with card 4242 4242 4242 4242
"""
import sys
import types
from itertools import count

_ids = count(1)
PIS, SESSIONS, REFUNDS = {}, {}, []


def _obj(**kw):
    return types.SimpleNamespace(**kw)


class StripeObject:
    """Like stripe-python 16's StripeObject: dict() on it raises, .to_dict() works."""
    def __init__(self, d):
        self._d = dict(d)

    def __iter__(self):
        raise TypeError("StripeObject is not iterable or a mapping; call .to_dict() for a plain dict.")

    def to_dict(self):
        return dict(self._d)


class PaymentIntent:
    @staticmethod
    def create(amount, **kw):
        pi = _obj(id=f"pi_{next(_ids)}", amount=amount, latest_charge=_obj(amount=amount, amount_refunded=0))
        PIS[pi.id] = pi
        return pi

    @staticmethod
    def retrieve(pi_id, expand=None):
        return PIS[pi_id]


class Session:
    @staticmethod
    def create(line_items, metadata, ui_mode="hosted", **kw):
        assert "payment_method_types" not in kw, "Stripe rejects payment_method_types on Checkout Sessions"
        assert ui_mode != "embedded", "Stripe renamed ui_mode 'embedded' to 'embedded_page'"
        if ui_mode == "embedded_page":
            assert "return_url" in kw and "success_url" not in kw, "embedded sessions take return_url only"
        else:
            assert "success_url" in kw, "hosted sessions need success_url"
        total = sum(li["price_data"]["unit_amount"] * li["quantity"] for li in line_items)
        sid = f"cs_test_{next(_ids)}"
        s = _obj(id=sid, status="open", payment_status="unpaid", amount_total=total, metadata=StripeObject(metadata),
                 payment_intent=None, line_items=line_items, ui_mode=ui_mode,
                 url=None if ui_mode == "embedded_page" else f"https://checkout.stripe.com/c/pay/{sid}",
                 client_secret=f"{sid}_secret_x" if ui_mode == "embedded_page" else None)
        SESSIONS[sid] = s
        return s

    @staticmethod
    def retrieve(sid):
        return SESSIONS[sid]

    @staticmethod
    def expire(sid):
        s = SESSIONS[sid]
        if s.status != "open":
            raise ValueError(f"Only open sessions can be expired (status {s.status})")
        s.status = "expired"
        return s


class Refund:
    @staticmethod
    def create(payment_intent, amount, **kw):
        ch = PIS[payment_intent].latest_charge
        if amount > ch.amount - ch.amount_refunded:
            raise ValueError(f"Refund amount ({amount}) is greater than unrefunded amount on charge")
        ch.amount_refunded += amount
        REFUNDS.append((payment_intent, amount))
        return _obj(id=f"re_{next(_ids)}")


def pay(session_id: str) -> None:
    """The traveller pays on Stripe with the test card."""
    s = SESSIONS[session_id]
    if s.status != "open":
        raise ValueError(f"session {session_id} is {s.status}")
    s.status, s.payment_status = "complete", "paid"
    s.payment_intent = PaymentIntent.create(s.amount_total).id


def install() -> None:
    sys.modules["stripe"] = _obj(PaymentIntent=PaymentIntent, Refund=Refund, checkout=_obj(Session=Session),
                                 api_key=None)
