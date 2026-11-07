"""End-to-end: one conversation through the real pipeline (intake → planner →
verifier → gate → locker room → vendors), checking the ledger after each turn.
Offline: mock SubQ/Gemini, in-memory Mongo. It's 2026-11-18 10:00, you're in Tokyo.

    python -m tests.test_e2e_chat
"""
import os

os.environ.setdefault("MOCK", "subq,gemini")
os.environ["DEMO_NOW"] = "2026-11-18T10:00:00"
os.environ["MONGODB_URI"] = ""

from common.db import ledger_col, plans_col  # noqa: E402
from orchestrator.pipeline import approve_and_execute, plan_trip  # noqa: E402
from seed.seed_japan import seed  # noqa: E402

TRIP = "JP-1115"
FAILS = []


def check(cond, what):
    print(f"    {'PASS' if cond else 'FAIL'}  {what}")
    if not cond:
        FAILS.append(what)


def active(item):
    return ledger_col().find_one({"type": "booking", "trip_id": TRIP, "id": item, "status": "active"})


def paid_total():
    return round(sum(p["amount_usd"] for p in ledger_col().find({"type": "payment", "trip_id": TRIP,
                                                                  "status": "completed"})), 2)


def say(text):
    msg, v = plan_trip(TRIP, text, trace=lambda *_: None)
    plan = plans_col().find_one({"trip_id": TRIP, "version": v})
    print(f"\n  You: {text}\n    → v{v} {[(a['op'], a['id'], a['cost_usd']) for a in plan['actions']]}")
    return msg, v


def approve(v):
    out = approve_and_execute(TRIP, v, "approve", trace=lambda *_: None)
    print("    → " + out.splitlines()[0])
    return out


def main():
    seed()
    msg, v = say("Japan Nov 15-22, Tokyo then Osaka for the dev events, budget $3,500")
    approve(v)
    check(paid_total() == 2910 and active("HTL-4471"), "booked, paid $2,910")

    msg, v = say("I love Osaka, stay 2 more nights in Osaka")
    check("You pay now: $490" in msg and "verified" in msg, "extend: +2 nights ($320) + return flight to Nov 24 ($170)")
    approve(v)
    h, f = active("HTL-4471"), active("FLT-2101")
    check(h["check_out"] == "2026-11-24" and f["date"] == "2026-11-24", "hotel and flight now end Nov 24")
    check(paid_total() == 3400, "paid $490 more, total $3,400")

    msg, v = say("Move my Osaka hotel one day later")
    check("can't be approved" in msg and "R-05" in msg, "shifting Osaka leaves Nov 19 without a hotel → blocked")
    out = approve_and_execute(TRIP, v, "approve", trace=lambda *_: None)
    check(out.startswith("Not approved") and active("HTL-4471")["check_in"] == "2026-11-19", "nothing changed")

    msg, v = say("Cancel my car rental")
    check("can't be approved" in msg and "R-15" in msg, "unknown booking → blocked, asks which booking")

    msg, v = say("Cancel my Osaka hotel please")
    check("no card needed" in msg and "Refunds to your card: $640" in msg,
          "cancel Osaka: 29h before check-in → lose 1 of 5 nights, refund $640")
    approve(v)
    check(active("HTL-4471") is None and paid_total() == 3400, "hotel cancelled, no new payment")
    check(active("HTL-2210") and active("FLT-2101"), "Tokyo hotel and flight untouched")

    msg, v = say("Cancel the whole trip")
    plan = plans_col().find_one({"trip_id": TRIP, "version": v})
    check(sorted(a["id"] for a in plan["actions"]) == ["DIN-102", "FLT-0915", "FLT-2101", "HTL-2210"],
          "whole trip: cancels the 4 remaining bookings")
    approve(v)
    check(ledger_col().count_documents({"type": "booking", "trip_id": TRIP, "status": "active"}) == 0,
          "no active bookings left")
    refunds = {r["id"]: r["amount_usd"] for r in ledger_col().find({"type": "refund", "trip_id": TRIP})}
    check(refunds.get("FLT-2101") == 870.0 and refunds.get("FLT-0915") == 0.0,
          "unflown return refunds $1,020 − $150 fee = $870; flown outbound refunds $0")

    msg, v = say("Japan Nov 15-22, Tokyo then Osaka, budget $3,500")
    check("verified" in msg, "trip fully cancelled → a fresh booking is allowed again")

    print("\n  ALL PASSED" if not FAILS else f"\n  {len(FAILS)} FAILED: {FAILS}")
    raise SystemExit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
