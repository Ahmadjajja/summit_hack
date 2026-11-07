"""P3 end-to-end tests with the real Fetch.ai uAgents + Agent Payment Protocol, offline.

    python run_booking_local.py          # all scenarios below, on one trip, in order
    python run_booking_local.py tamper   # a vendor overcharges → the locker room refuses

Scenarios (it's 2026-11-18 10:00, you're in Tokyo):
  1 book        plan + verify + approve, then the agents book everything
  2 reschedule  move the Osaka stay 2 days later + move the return flight (pay only the change fee)
  3 extend      1 more night in Osaka + move the return flight again (pay only the extras)
  4 cancel      cancel the Osaka hotel + the past dinner; refunds follow vendor policy,
                and a refund that differs from the plan is flagged
  5 failsafe    a change whose new booking fails → the old booking is NOT cancelled

Runs fully offline: in-memory Mongo, mock SubQ/Gemini, mock payments (BOOKING_DRY_RUN).
Change plans are built here with vendors/policy.py; in the product P1's planner builds them.
"""
import asyncio
import os
import sys

os.environ.setdefault("MOCK", "subq,gemini")
os.environ.setdefault("DEMO_NOW", "2026-11-18T10:00:00")
os.environ["MONGODB_URI"] = ""  # always in-memory for tests

from uagents import Agent, Bureau, Context  # noqa: E402

from common.contracts import Action, PlanPackage  # noqa: E402
from common.db import clean, ledger_col, plans_col  # noqa: E402
from gate import gate  # noqa: E402
from orchestrator.pipeline import plan_trip  # noqa: E402
from seed.seed_japan import seed  # noqa: E402
from vendors import agents as booking  # noqa: E402
from vendors import policy  # noqa: E402
from vendors.protocol import ExecutePlan, PlanExecuted  # noqa: E402

TRIP = "JP-1115"
TAMPER = "tamper" in sys.argv[1:]
FAILS: list[str] = []


def check(cond: bool, what: str):
    print(f"   {'PASS' if cond else 'FAIL'}  {what}")
    if not cond:
        FAILS.append(what)


def booking_doc(item_id: str, status: str = "active") -> dict | None:
    return clean(ledger_col().find_one({"type": "booking", "trip_id": TRIP, "id": item_id, "status": status}))


def paid_for(version: int) -> float:
    return round(sum(p["amount_usd"] for p in ledger_col().find(
        {"type": "payment", "trip_id": TRIP, "version": version, "status": "completed"})), 2)


def approved_change_plan(actions: list[Action]) -> tuple[int, str]:
    """Store a change plan as the next version, approved, like gate.approve() would."""
    version = max(p["version"] for p in plans_col().find({"trip_id": TRIP})) + 1
    plan = PlanPackage(trip_id=TRIP, version=version, intent="reschedule", slots=[], actions=actions,
                       total_usd=policy.net_delta(actions), money_delta_usd=policy.net_delta(actions))
    plan.plan_hash, plan.approved = gate.plan_hash(plan), True
    plans_col().insert_one(plan.model_dump())
    return version, plan.plan_hash


# ---------------------------------------------------------------- scenarios
def s_book():
    _, version = plan_trip(TRIP, "Japan Nov 15-22, Tokyo then Osaka, budget $3,500", trace=lambda *_: None)
    gate.approve(TRIP, version, "approve")
    plan = plans_col().find_one({"trip_id": TRIP, "version": version})
    print(f"   approved plan v{version}: ${plan['total_usd']:,.0f}")
    return version, plan["plan_hash"]


def v_book(version):
    check(paid_for(version) == 2910.0, f"paid exactly $2,910 (got ${paid_for(version):,.2f})")
    check(all(booking_doc(i) for i in ["FLT-0915", "FLT-2101", "HTL-2210", "HTL-4471", "DIN-102"]),
          "5 active bookings")


def s_reschedule():
    acts = [policy.shift_hotel_action(TRIP, "HTL-4471", 2),
            policy.change_flight_action(TRIP, "FLT-2101", "FLT-2190")]
    print(f"   actions: {[(a.op, a.id, a.cost_usd) for a in acts]}")
    return approved_change_plan(acts)


def v_reschedule(version):
    h, f = booking_doc("HTL-4471"), booking_doc("FLT-2101")
    check(h["check_in"] == "2026-11-21" and h["check_out"] == "2026-11-24", "Osaka hotel moved to Nov 21–24")
    check(f["date"] == "2026-11-24" and f["flight_no"] == "FLT-2190", "return flight moved to FLT-2190 on Nov 24")
    check(paid_for(version) == 170.0, f"paid only the change: $150 fee + $20 fare diff = $170 (got ${paid_for(version)})")


def s_extend():
    acts = [policy.extend_hotel_action(TRIP, "HTL-4471", 1),
            policy.change_flight_action(TRIP, "FLT-2101", "FLT-2195")]
    print(f"   actions: {[(a.op, a.id, a.cost_usd) for a in acts]}")
    return approved_change_plan(acts)


def v_extend(version):
    h, f = booking_doc("HTL-4471"), booking_doc("FLT-2101")
    check(h["check_out"] == "2026-11-25" and h["cost_usd"] == 640.0, "Osaka hotel now 4 nights, $640")
    check(f["date"] == "2026-11-25", "return flight moved to Nov 25")
    check(paid_for(version) == 310.0, f"paid $160 night + $150 fee = $310 (got ${paid_for(version)})")


def s_cancel():
    hotel = policy.cancel_action(TRIP, "HTL-4471")                          # computed from policy
    dinner = Action(op="cancel", id="DIN-102", cost_usd=-60.0)              # planner wrongly expects $60
    print(f"   actions: {[(a.op, a.id, a.cost_usd) for a in (hotel, dinner)]}")
    return approved_change_plan([hotel, dinner])


def v_cancel(version):
    r = {x["id"]: x for x in ledger_col().find({"type": "refund", "trip_id": TRIP})}
    check(booking_doc("HTL-4471", "cancelled") is not None, "Osaka hotel cancelled")
    check(r["HTL-4471"]["amount_usd"] == 640.0, "check-in Nov 21 is 77h away (≥72h) → full $640 refund")
    check(r["DIN-102"]["amount_usd"] == 0.0 and not r["DIN-102"]["matches_plan"],
          "past dinner refunds $0 and the $60 the plan expected is flagged")
    check(paid_for(version) == 0.0, "no card used for a cancel")


def s_failsafe():
    acts = [Action(op="book", id="HTL-9999", cost_usd=200.0),    # new hotel that doesn't exist
            policy.cancel_action(TRIP, "HTL-2210")]               # old Tokyo hotel
    print(f"   actions: {[(a.op, a.id, a.cost_usd) for a in acts]}")
    return approved_change_plan(acts)


def v_failsafe(version):
    check(booking_doc("HTL-2210") is not None, "old Tokyo hotel still active (cancel skipped)")
    check(ledger_col().count_documents({"type": "payment", "trip_id": TRIP, "version": version}) == 0,
          "no payment taken for the booking that couldn't happen")


SCENARIOS = [("book", s_book, v_book), ("reschedule", s_reschedule, v_reschedule),
             ("extend", s_extend, v_extend), ("cancel", s_cancel, v_cancel),
             ("failsafe", s_failsafe, v_failsafe)]


def main():
    seed()
    if TAMPER:  # the hotel vendor sees an inflated price, the locker room checks Mongo
        orig = booking._approved_plan

        def overcharged(t, v):
            p = orig(t, v)
            if p:
                for a in p.actions:
                    if a.id.startswith("HTL"):
                        a.cost_usd += 500
            return p
        booking._approved_plan = overcharged
        scenarios = [("tamper", s_book, lambda v: (
            check(paid_for(v) == 1710.0, f"only flights + dinner paid: $1,710 (got ${paid_for(v):,.2f})"),
            check(booking_doc("HTL-2210") is None, "no hotel booked at the inflated price")))]
    else:
        scenarios = SCENARIOS

    agents = booking.build_agents()
    client = Agent(name="test-orchestrator", seed="tripcheck-test-orchestrator")
    state = {"i": 0, "version": None}

    async def run_next(ctx: Context):
        if state["i"] >= len(scenarios):
            print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
            os._exit(1 if FAILS else 0)
        name, start, _ = scenarios[state["i"]]
        print(f"\n== {state['i'] + 1}. {name}")
        version, h = start()
        state["version"] = version
        await ctx.send(agents["coordinator"].address, ExecutePlan(trip_id=TRIP, version=version, plan_hash=h))

    @client.on_event("startup")
    async def go(ctx: Context):
        await asyncio.sleep(1)
        await run_next(ctx)

    @client.on_message(PlanExecuted)
    async def got(ctx: Context, sender: str, m: PlanExecuted):
        print("   chat ← " + m.summary.replace("\n", "\n          "))
        scenarios[state["i"]][2](state["version"])
        state["i"] += 1
        await run_next(ctx)

    Bureau(agents=list(agents.values()) + [client], port=8099).run()


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
