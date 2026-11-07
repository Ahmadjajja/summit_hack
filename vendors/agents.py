"""⑥ Booking agents on Fetch.ai (owner: P3).

  orchestrator ──ExecutePlan──▶ coordinator ──VendorOrder──▶ flight / hotel / dining
                                                               │  RequestPayment ▼
                                                           locker room (checks Mongo)
                                                               │  CommitPayment  ▲
  orchestrator ◀─PlanExecuted── coordinator ◀─VendorResult── vendor books → ledger

Run all five agents together:
    python -m vendors.agents            # prints the coordinator address for P1
Set AGENTS_USE_MAILBOX=true (on a laptop with internet) to register them on
Agentverse so they show up there.
"""
import asyncio

from uagents import Agent, Bureau, Context, Protocol
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    Funds,
    RejectPayment,
    RequestPayment,
    payment_protocol_spec,
)

from common import config
from common.contracts import PlanPackage
from common.db import clean, plans_col
from locker.agent import make_locker
from vendors import vendors
from vendors.protocol import ExecutePlan, PlanExecuted, VendorOrder, VendorResult

VENDORS = ["flight", "hotel", "dining"]
PREFIX = {"flight": "FLT", "hotel": "HTL", "dining": "DIN"}
BOOKING = Protocol(name="TripCheckBooking", version="0.1.0")


def _kwargs(name: str, port: int) -> dict:
    kw = {"seed": f"{config.AGENT_SEED_PHRASE}-{name}"}
    if config.AGENTS_USE_MAILBOX:
        kw.update(port=port, mailbox=True)
    return kw


def _approved_plan(trip_id: str, version: int) -> PlanPackage | None:
    doc = plans_col().find_one({"trip_id": trip_id, "version": version})
    return PlanPackage(**clean(doc)) if doc and doc.get("approved") else None


# ---------------------------------------------------------------- vendors
def make_vendor(kind: str, port: int, locker_address_ref: dict, coordinator_ref: dict) -> Agent:
    agent = Agent(name=f"tripcheck-{kind}-vendor", **_kwargs(kind, port))
    orders: dict[str, VendorOrder] = {}       # payment reference -> order
    book_proto = Protocol(name=f"TripCheck{kind.title()}Vendor", version="0.1.0")
    pay_proto = Protocol(spec=payment_protocol_spec, role="seller")

    def _finish(order: VendorOrder, payment_id: str | None) -> VendorResult:
        plan = _approved_plan(order.trip_id, order.version)
        costs = {a.id: a.cost_usd for a in plan.actions} if plan else {}
        lines = []
        for item in order.item_ids:
            if order.op == "book":
                r = vendors.book(order.trip_id, item, costs.get(item, 0.0))
                lines.append(f"booked {item} · conf {r['confirmation']}")
            else:
                r = vendors.cancel(order.trip_id, item, costs.get(item, 0.0))
                lines.append(f"cancelled {item} · refund ${r['amount_usd']:,.0f} to original card")
        return VendorResult(trip_id=order.trip_id, version=order.version, vendor=kind,
                            ok=True, lines=lines, payment_id=payment_id)

    @book_proto.on_message(VendorOrder)
    async def on_order(ctx: Context, sender: str, order: VendorOrder):
        plan = _approved_plan(order.trip_id, order.version)
        if plan is None or plan.plan_hash != order.plan_hash:
            await ctx.send(sender, VendorResult(trip_id=order.trip_id, version=order.version, vendor=kind,
                                                ok=False, lines=["refused: plan not approved"]))
            return
        costs = {a.id: a.cost_usd for a in plan.actions}
        amount = round(sum(costs.get(i, 0.0) for i in order.item_ids), 2)
        if order.op != "book" or amount <= 0:   # cancels and free items need no money
            await ctx.send(sender, _finish(order, None))
            return
        ref = f"{order.trip_id}:v{order.version}:{kind}"
        orders[ref] = order
        ctx.logger.info(f"requesting ${amount:,.2f} from locker room for {order.item_ids}")
        await ctx.send(locker_address_ref["address"], RequestPayment(
            accepted_funds=[Funds(amount=f"{amount:.2f}", currency="USD", payment_method="stripe_test")],
            recipient=agent.address, deadline_seconds=300, reference=ref,
            description=f"TripCheck {kind} booking {', '.join(order.item_ids)}",
            metadata={"trip_id": order.trip_id, "version": str(order.version), "plan_hash": order.plan_hash,
                      "vendor": kind, "items": ",".join(order.item_ids)}))

    @pay_proto.on_message(CommitPayment)
    async def on_commit(ctx: Context, sender: str, msg: CommitPayment):
        order = orders.pop(msg.reference or "", None)
        if order is None:
            await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason="unknown order"))
            return
        try:
            result = _finish(order, msg.transaction_id)
        except Exception as e:  # booking failed after payment: void the payment
            await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason=str(e)))
            result = VendorResult(trip_id=order.trip_id, version=order.version, vendor=kind,
                                  ok=False, lines=[f"booking failed: {e}"])
        else:
            await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
        await ctx.send(coordinator_ref["address"], result)

    @pay_proto.on_message(RejectPayment)
    async def on_reject(ctx: Context, sender: str, msg: RejectPayment):
        # The reference isn't on RejectPayment; with one open order per vendor it's the pending one.
        for ref, order in list(orders.items()):
            orders.pop(ref)
            await ctx.send(coordinator_ref["address"], VendorResult(
                trip_id=order.trip_id, version=order.version, vendor=kind, ok=False,
                lines=[f"locker room refused payment: {msg.reason}"]))

    agent.include(book_proto)
    agent.include(pay_proto, publish_manifest=True)
    return agent


# ---------------------------------------------------------------- coordinator
def make_coordinator(port: int, vendor_addresses: dict[str, str]) -> Agent:
    agent = Agent(name="tripcheck-booking-coordinator", **_kwargs("coordinator", port))
    jobs: dict[str, dict] = {}   # "trip:v" -> {reply_to, waiting, results}

    @BOOKING.on_message(ExecutePlan)
    async def on_execute(ctx: Context, sender: str, msg: ExecutePlan):
        plan = _approved_plan(msg.trip_id, msg.version)
        if plan is None or plan.plan_hash != msg.plan_hash:
            await ctx.send(sender, PlanExecuted(trip_id=msg.trip_id, version=msg.version, ok=False,
                                                summary="Refused: plan is not approved or has changed."))
            return
        orders: dict[tuple[str, str], list[str]] = {}
        for a in plan.actions:
            kind = next((k for k, p in PREFIX.items() if a.id.startswith(p)), None)
            if kind and a.op in ("book", "cancel"):
                orders.setdefault((kind, a.op), []).append(a.id)
        key = f"{msg.trip_id}:v{msg.version}"
        jobs[key] = {"reply_to": sender, "waiting": len(orders), "results": []}
        ctx.logger.info(f"executing {key}: {len(orders)} vendor order(s)")
        for (kind, op), ids in orders.items():
            await ctx.send(vendor_addresses[kind], VendorOrder(
                trip_id=msg.trip_id, version=msg.version, plan_hash=msg.plan_hash, op=op, item_ids=ids))

    @BOOKING.on_message(VendorResult)
    async def on_result(ctx: Context, sender: str, msg: VendorResult):
        key = f"{msg.trip_id}:v{msg.version}"
        job = jobs.get(key)
        if not job:
            return
        job["results"].append(msg)
        job["waiting"] -= 1
        if job["waiting"] > 0:
            return
        jobs.pop(key)
        ok = all(r.ok for r in job["results"])
        lines = [f"{'✅ Done' if ok else '⚠️ Partly done'} · trip {msg.trip_id} (plan v{msg.version})"]
        for r in sorted(job["results"], key=lambda r: r.vendor):
            lines += [f"- {r.vendor}: {line}" for line in r.lines]
        paid = [r for r in job["results"] if r.payment_id]
        if paid:
            lines.append(f"Paid through the locker room: {', '.join(r.payment_id for r in paid)}")
        await ctx.send(job["reply_to"], PlanExecuted(trip_id=msg.trip_id, version=msg.version,
                                                     ok=ok, summary="\n".join(lines)))

    agent.include(BOOKING, publish_manifest=True)
    return agent


# ---------------------------------------------------------------- wiring
def build_agents() -> dict[str, Agent]:
    """Create all five booking-side agents with each other's addresses wired in."""
    locker_ref, coord_ref = {}, {}
    vendors_ = {k: make_vendor(k, 8102 + i, locker_ref, coord_ref) for i, k in enumerate(VENDORS)}
    addresses = {k: a.address for k, a in vendors_.items()}
    locker = make_locker(_kwargs("locker", 8101), addresses)
    coordinator = make_coordinator(8100, addresses)
    locker_ref["address"], coord_ref["address"] = locker.address, coordinator.address
    return {"coordinator": coordinator, "locker": locker, **vendors_}


def main():
    from seed.seed_japan import seed
    seed(reset=not config.MONGODB_URI)
    agents = build_agents()
    for name, a in agents.items():
        print(f"{name:12s} {a.address}")
    print("\nP1: send ExecutePlan(trip_id, version, plan_hash) to the coordinator address above.")
    bureau = Bureau(agents=list(agents.values()), port=8099 if not config.AGENTS_USE_MAILBOX else None)
    bureau.run()


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
