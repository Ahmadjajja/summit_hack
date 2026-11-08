"""Live web search (Fetch.ai ASI:One) and Stripe Checkout (test mode), offline.

ASI:One runs on its mock (MOCK=asi) and Stripe is a fake module that behaves like
the test API, so this needs no keys and spends nothing.

    python -m tests.test_search_and_checkout
"""
import os

os.environ["MOCK"] = "subq,gemini,asi"
os.environ["MONGODB_URI"] = ""
os.environ["STRIPE_TEST_KEY"] = "sk_test_fake"
os.environ["BOOKING_DRY_RUN"] = "false"
os.environ["DEMO_NOW"] = "2026-11-01T10:00:00"

from tests import fake_stripe  # noqa: E402
from tests.fake_stripe import REFUNDS, SESSIONS  # noqa: E402

fake_stripe.install()
pay_on_stripe = fake_stripe.pay
_PI, _Session = fake_stripe.PaymentIntent, fake_stripe.Session

from common.db import candidates_col, ledger_col, requests_col  # noqa: E402
from locker import locker  # noqa: E402
from orchestrator.pipeline import plan_trip  # noqa: E402
from scout import scout  # noqa: E402
from scout.asi_search import parse_json  # noqa: E402
from seed.seed_japan import seed  # noqa: E402
import web  # noqa: E402

FAILS = []


def check(cond, what):
    print(f"  {'PASS' if cond else 'FAIL'}  {what}")
    if not cond:
        FAILS.append(what)


def main():
    seed()

    print("search helpers")
    check(parse_json('Sure!\n```json\n{"a": [1]}\n```') == {"a": [1]}, "JSON read from a fenced reply")
    check(parse_json('here: [{"x": 1}] done') == [{"x": 1}], "JSON read from plain text")
    check(scout.split_nights(["Tokyo", "Osaka"], "2026-11-15", "2026-11-22")
          == [("Tokyo", "2026-11-15", "2026-11-19"), ("Osaka", "2026-11-19", "2026-11-22")],
          "nights split like the seeded demo (4 + 3)")
    bad = scout.hotel_candidates([{"name": "X", "nightly_usd": "n/a"}, {"name": "", "nightly_usd": 100},
                                  {"name": "Good", "nightly_usd": "$120", "rating": 9}], "Rome",
                                 "2026-12-01", "2026-12-03", "web:asi1")
    check(len(bad) == 1 and bad[0]["nightly_usd"] == 120 and bad[0]["rating"] == 5.0,
          "results with no price or name are dropped; prices and ratings are cleaned")
    fl = scout.flight_candidates([{"airline": "A", "dep": "25:00", "price_usd": 300},
                                  {"airline": "B", "dep": "9:05", "price_usd": 300}], "flight_out",
                                 "2026-12-01", "JFK", "Rome", "web:asi1")
    check([f["dep"] for f in fl] == ["09:05"], "flights with an impossible departure time are dropped")

    print("unknown destination → live search → normal plan")
    msg, v = plan_trip("T-PARIS", "Trip to Paris Dec 10-14 from JFK with a nice dinner, budget $3,000",
                       trace=lambda *_: None)
    req = requests_col().find_one({"trip_id": "T-PARIS"})
    check(req["cities"] == ["Paris"] and "cities" not in req["defaults_used"],
          f"destination is Paris, not a silent Tokyo default (got {req['cities']})")
    check(req["origin"] == "JFK", "origin read from 'from JFK'")
    check("verified ✅" in msg and "live web price" in msg and "[source](" in msg,
          "plan verified and each live price shows its source")
    check(candidates_col().count_documents({"source": "mock-web"}) > 0, "search results saved as candidates")
    check("FLT-0915" not in msg and "Tokyo" not in msg, "no Japan flights or hotels in a Paris plan")

    print("seeded Japan trip never searches")
    before = candidates_col().count_documents({})
    msg, v = plan_trip("T-JP", "Japan Nov 15-22, Tokyo then Osaka for the dev events, budget $3,500",
                       trace=lambda *_: None)
    check(candidates_col().count_documents({}) == before and "live web" not in msg, "no search, same demo")

    print("search finds nothing → clear message, no loop")
    orig = scout._mock_stay
    scout._mock_stay = lambda *a: {"hotels": [], "dinners": []}
    try:
        msg, _ = plan_trip("T-NONE", "Trip to Reykjavik Dec 2-4, budget $2,000", trace=lambda *_: None)
    finally:
        scout._mock_stay = orig
    check("couldn't find bookable options" in msg and "a hotel in Reykjavik" in msg,
          "says what's missing instead of 3 loops of 'which booking is it about?'")

    print("Stripe Checkout (test mode)")
    web.STATE.update(trip_id="T-PARIS", awaiting=None, checkout=None, notice=None)
    res = web.on_message("Trip to Paris Dec 10-14 from JFK with a nice dinner, budget $3,000")
    check(res["can_approve"], "plan can be approved")
    res = web.on_approve()
    co = web.STATE["checkout"]
    check(bool(co) and "checkout.stripe.com" in res["reply"], "approve opens Stripe Checkout instead of charging")
    s = SESSIONS[co["session_id"]]
    plan_total = sum(li["price_data"]["unit_amount"] for li in s.line_items) / 100
    check(abs(plan_total - co["amount_usd"]) < 0.01 and len(s.line_items) == 4,
          f"one line item per booking, total ${co['amount_usd']:,.2f}")
    check(ledger_col().count_documents({"type": "booking", "trip_id": "T-PARIS"}) == 0, "nothing booked before paying")
    title, _ = web.on_stripe_return({"session_id": [co["session_id"]]})
    check(title == "Not paid yet" and web.STATE["checkout"], "returning without paying books nothing")
    pay_on_stripe(co["session_id"])
    title, _ = web.on_stripe_return({"session_id": [co["session_id"]]})
    check(title == "Payment received", "paid session is confirmed with Stripe")
    pays = list(ledger_col().find({"type": "payment", "trip_id": "T-PARIS"}))
    pi = s.payment_intent
    check(pays and all(p["payment_id"].startswith(pi + "#") for p in pays),
          "vendor payments are allocations of the one Checkout payment")
    check(abs(sum(p["amount_usd"] for p in pays) - co["amount_usd"]) < 0.01, "allocations add up to what was paid")
    check(len({p["payment_id"] for p in pays}) == len(pays), "each allocation has its own ledger ID")
    check(ledger_col().count_documents({"type": "booking", "trip_id": "T-PARIS", "status": "active"}) == 4,
          "all 4 bookings made")
    bks = list(ledger_col().find({"type": "booking", "trip_id": "T-PARIS"}))
    check(all(b["payment_ids"] == [f"{pi}#{b['vendor']}"] for b in bks),
          "each booking records its own vendor's payment (refunds go to the right one)")
    check(web.STATE["notice"] and "Stripe (test mode) confirmed" in web.STATE["notice"], "chat gets the result")
    title, _ = web.on_stripe_return({"session_id": [co["session_id"]]})
    check(title == "Nothing to confirm", "a second return doesn't book twice")

    print("extending a live-search trip searches for the new return flight")
    res = web.on_message("Stay 1 more night in Paris")
    check(res["can_approve"] and "change FLT-W" in res["reply"],
          "return flight on the new date found by search, change verified")
    res = web.on_approve()
    co2 = web.STATE["checkout"]
    check(bool(co2) and co2["session_id"] != co["session_id"], "the change gets its own Stripe Checkout")
    pay_on_stripe(co2["session_id"])
    title, _ = web.on_stripe_return({"session_id": [co2["session_id"]]})
    check(title == "Payment received" and "changed" in web.STATE["notice"], "extension paid and applied")

    print("refunds go back to the Checkout payment")
    hotel = ledger_col().find_one({"type": "booking", "trip_id": "T-PARIS", "kind": "hotel"})
    pi2 = SESSIONS[co2["session_id"]].payment_intent
    n = len(REFUNDS)
    ref = locker.refund_to_card(hotel["payment_ids"], hotel["cost_usd"], "T-PARIS")
    night = hotel["nightly_usd"]
    check(ref.startswith("re_") and REFUNDS[n:] == [(pi2, round(night * 100)), (pi, round((hotel["cost_usd"] - night) * 100))],
          "extended hotel's refund: the extra night from the change payment, the rest from the booking payment")
    # booking paid twice (book + extension): refund spreads over both payments
    a, b = _PI.create(10000).id, _PI.create(3000).id
    locker.refund_to_card([a, b], 120.0, "T-X")
    check(REFUNDS[-2:] == [(b, 3000), (a, 9000)], "refund larger than the last payment is split, newest first")
    try:
        locker.refund_to_card([b], 50.0, "T-X")
        check(False, "over-refund refused")
    except locker.LockerRefused:
        check(True, "over-refund refused")

    print("tampered Checkout is refused")
    s2 = _Session.create(line_items=[{"quantity": 1, "price_data": {"unit_amount": 100}}],
                         metadata={"trip_id": "T-PARIS", "plan_hash": "nope"}, success_url="x")
    ledger_col().insert_one({"type": "checkout", "trip_id": "T-PARIS", "version": pays[0]["version"],
                             "session_id": s2.id, "amount_usd": 1.0, "plan_hash": pays[0]["plan_hash"],
                             "status": "open"})
    pay_on_stripe(s2.id)
    try:
        locker.confirm_checkout(s2.id)
        check(False, "checkout for another plan refused")
    except locker.LockerRefused as e:
        check(True, f"checkout for another plan refused ({e})")

    print("\nALL PASSED" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    raise SystemExit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
