"""P3 end-to-end test: plan + approve a trip, then let the Fetch.ai booking agents
execute it (coordinator → vendors → locker room via the Agent Payment Protocol).

    python run_booking_local.py          # happy path
    python run_booking_local.py tamper   # vendor asks for more than approved → locker refuses

Runs fully offline: in-memory Mongo, mock SubQ/Gemini, mock payments.
"""
import asyncio
import os
import sys

os.environ.setdefault("MOCK", "subq,gemini")

from uagents import Agent, Bureau, Context  # noqa: E402

from common.db import ledger_col, plans_col  # noqa: E402
from orchestrator.pipeline import plan_trip  # noqa: E402
from gate import gate  # noqa: E402
from seed.seed_japan import seed  # noqa: E402
from vendors import agents as booking  # noqa: E402
from vendors.protocol import ExecutePlan, PlanExecuted  # noqa: E402

TAMPER = "tamper" in sys.argv[1:]


def main():
    seed()
    trip_id = "JP-1115"
    msg, version = plan_trip(trip_id, "Japan Nov 15-22, Tokyo then Osaka, budget $3,500", trace=lambda *_: None)
    gate.approve(trip_id, version, "approve")
    plan = plans_col().find_one({"trip_id": trip_id, "version": version})
    print(f"Approved plan v{version} · total ${plan['total_usd']:,.0f} · hash {plan['plan_hash']}\n")

    if TAMPER:  # simulate a vendor overcharging: raise the hotel price after approval
        booking._approved_plan_orig = booking._approved_plan

        def overcharged(t, v):
            p = booking._approved_plan_orig(t, v)
            if p:
                for a in p.actions:
                    if a.id.startswith("HTL"):
                        a.cost_usd += 500
            return p
        booking._approved_plan = overcharged

    agents = booking.build_agents()
    client = Agent(name="test-orchestrator", seed="tripcheck-test-orchestrator")
    done = {}

    @client.on_event("startup")
    async def go(ctx: Context):
        await asyncio.sleep(1)
        await ctx.send(agents["coordinator"].address,
                       ExecutePlan(trip_id=trip_id, version=version, plan_hash=plan["plan_hash"]))

    @client.on_message(PlanExecuted)
    async def got(ctx: Context, sender: str, m: PlanExecuted):
        print("\n--- PlanExecuted (what P1 posts to chat) ---\n" + m.summary)
        done["ok"] = m.ok
        pays = list(ledger_col().find({"type": "payment"}))
        print("\nLedger payments:")
        for p in pays:
            print(f"  {p['vendor']:7s} ${p['amount_usd']:>8,.2f}  {p['status']:9s} {p['payment_id']}")
        print(f"  total paid ${sum(p['amount_usd'] for p in pays):,.2f} of approved ${plan['money_delta_usd']:,.2f}")
        os._exit(0 if (m.ok or TAMPER) else 1)

    bureau = Bureau(agents=list(agents.values()) + [client], port=8099)
    bureau.run()


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
