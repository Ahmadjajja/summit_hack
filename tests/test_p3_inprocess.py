"""P3 checks on the in-process path (no agents): refund policy edge cases,
locker room refusals, and change plans through run_locker + run_execute.

    python -m tests.test_p3_inprocess
"""
import os
from datetime import datetime

os.environ.setdefault("MOCK", "subq,gemini")
os.environ["DEMO_NOW"] = "2026-11-18T10:00:00"
os.environ["MONGODB_URI"] = ""

from common.contracts import Action, PlanPackage, Signal  # noqa: E402
from common.db import clean, ledger_col, plans_col, verdicts_col  # noqa: E402
from gate import gate  # noqa: E402
from locker.locker import LockerRefused, authorize_and_charge, run_locker  # noqa: E402
from orchestrator.pipeline import approve_and_execute, plan_trip  # noqa: E402
from seed.seed_japan import seed  # noqa: E402
from vendors import policy  # noqa: E402
from vendors.vendors import run_execute  # noqa: E402

TRIP = "JP-1115"
FAILS = []


def check(cond, what):
    print(f"  {'PASS' if cond else 'FAIL'}  {what}")
    if not cond:
        FAILS.append(what)


def refused(fn) -> str | None:
    try:
        fn()
    except LockerRefused as e:
        return str(e)
    return None


def change(actions):
    v = max(p["version"] for p in plans_col().find({"trip_id": TRIP})) + 1
    plan = PlanPackage(trip_id=TRIP, version=v, intent="extend", slots=[], actions=actions,
                       total_usd=policy.net_delta(actions), money_delta_usd=policy.net_delta(actions))
    plans_col().insert_one(plan.model_dump())
    check(gate.approve(TRIP, v, "approve") is None, f"v{v} can't be approved before it's verified")
    verdicts_col().insert_one({"trip_id": TRIP, "version": v, "result": "PASS", "hard_failures": 0})
    sig = gate.approve(TRIP, v, "approve")
    if sig.type == "pay":
        sig = run_locker(sig)
    return v, run_execute(sig)


def main():
    seed()
    print("refund policy")
    hotel = {"kind": "hotel", "check_in": "2026-11-19", "check_out": "2026-11-22", "nightly_usd": 160,
             "cost_usd": 480, "policy": {"free_cancel_hours": 72, "penalty_nights": 1}}
    check(policy.refund_for(hotel, datetime(2026, 11, 15, 12)) == 480, "≥72h before check-in → full refund")
    check(policy.refund_for(hotel, datetime(2026, 11, 18, 10)) == 320, "29h before → lose 1 night ($320)")
    check(policy.refund_for(hotel, datetime(2026, 11, 20, 9)) == 160, "mid-stay Nov 20 → tonight charged, refund Nov 21 ($160)")
    check(policy.refund_for(hotel, datetime(2026, 11, 22, 9)) == 0, "checkout day → $0")
    flight = {"kind": "flight", "date": "2026-11-22", "dep": "14:00", "cost_usd": 850}
    check(policy.refund_for(flight, datetime(2026, 11, 18)) == 700, "flight cancel → price minus $150 fee")
    check(policy.refund_for(flight, datetime(2026, 11, 22, 15)) == 0, "after departure → $0")

    print("book via in-process pipeline")
    _, version = plan_trip(TRIP, "Japan Nov 15-22, Tokyo then Osaka, budget $3,500", trace=lambda *_: None)
    out = approve_and_execute(TRIP, version, "approve", trace=lambda *_: None)
    check("booked HTL-4471" in out and "Paid $2,910" in out, "booked all, paid $2,910")

    print("locker room refusals")
    plan = clean(plans_col().find_one({"trip_id": TRIP, "version": version}))
    h = plan["plan_hash"]
    check(refused(lambda: authorize_and_charge(TRIP, version, h, "hotel", ["HTL-4471"], 480, "r1")) is not None,
          f"second charge beyond the approved total refused")
    check("not in the approved plan" in (refused(lambda: authorize_and_charge(
        TRIP, version, h, "hotel", ["HTL-2290"], 960, "r2")) or ""), "item not in plan refused")
    check("cannot charge" in (refused(lambda: authorize_and_charge(
        TRIP, version, h, "hotel", ["FLT-0915"], 800, "r3")) or ""), "hotel vendor charging for a flight refused")
    check("different plan hash" in (refused(lambda: authorize_and_charge(
        TRIP, version, "bad", "hotel", ["HTL-4471"], 480, "r4")) or ""), "wrong plan hash refused")
    plans_col().update_one({"trip_id": TRIP, "version": version}, {"$set": {"actions.0.cost_usd": 1.0}})
    check("hash mismatch" in (refused(lambda: run_locker(Signal(type="pay", trip_id=TRIP, version=version,
                                                                 plan_hash=h))) or ""),
          "plan edited after approval refused")
    plans_col().update_one({"trip_id": TRIP, "version": version}, {"$set": {"actions.0.cost_usd": plan["actions"][0]["cost_usd"]}})

    print("extend in-process")
    v, out = change([policy.extend_hotel_action(TRIP, "HTL-4471", 2),
                     policy.change_flight_action(TRIP, "FLT-2101", "FLT-2190")])
    b = clean(ledger_col().find_one({"type": "booking", "id": "HTL-4471", "status": "active"}))
    check(b["check_out"] == "2026-11-24", "hotel extended to Nov 24")
    check("Paid $490" in out, "paid $320 nights + $170 flight change = $490")

    print("cancel in-process (no locker room)")
    v, out = change([policy.cancel_action(TRIP, "HTL-4471")])
    check("refund $640" in out, "refund issued: check-in Nov 19 → 24 stay, 29h away → lose 1 night of 5 = $640")
    check(ledger_col().count_documents({"type": "payment", "version": v}) == 0, "no payment for a cancel")

    print("failed change keeps old booking")
    v, out = change([Action(op="book", id="HTL-9999", cost_usd=100), policy.cancel_action(TRIP, "HTL-2210")])
    check("Nothing changed" in out, "refused up front")
    check(ledger_col().find_one({"type": "booking", "id": "HTL-2210", "status": "active"}) is not None,
          "old Tokyo hotel still active")

    print("\nALL PASSED" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    raise SystemExit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
