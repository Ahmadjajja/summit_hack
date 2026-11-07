"""⑥ Booking agents on Fetch.ai (owner: P3).

  orchestrator ──ExecutePlan──▶ coordinator ──VendorOrder──▶ flight / hotel / dining
                                                               │  RequestPayment ▼
                                                           locker room (checks Mongo)
                                                               │  CommitPayment  ▲
  orchestrator ◀─PlanExecuted── coordinator ◀─VendorResult── vendor books → ledger

Run all five agents together:
    python -m vendors.agents            # prints the coordinator address for P1
Set BOOKING_AGENTS_MAILBOX=true (with internet) to also register them on
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
    if config.BOOKING_AGENTS_MAILBOX:
        kw.update(port=port, mailbox=True)
    return kw


def _approved_plan(trip_id: str, version: int) -> PlanPackage | None:
    doc = plans_col().find_one({"trip_id": trip_id, "version": version})
    return PlanPackage(**clean(doc)) if doc and doc.get("approved") else None


# ---------------------------------------------------------------- vendors
def _actions_for(plan: PlanPackage, item_ids: list[str]):
    by_id = {a.id: a for a in plan.actions}
    return [by_id[i] for i in item_ids if i in by_id]


def make_vendor(kind: str, port: int, locker_address_ref: dict, coordinator_ref: dict) -> Agent:
    agent = Agent(name=f"tripcheck-{kind}-vendor", **_kwargs(kind, port))
    orders: dict[str, VendorOrder] = {}       # payment reference -> order waiting for money
    book_proto = Protocol(name=f"TripCheck{kind.title()}Vendor", version="0.1.0")
    pay_proto = Protocol(spec=payment_protocol_spec, role="seller")

    def _finish(order: VendorOrder, payment_id: str | None) -> VendorResult:
        plan = _approved_plan(order.trip_id, order.version)
        lines, ok = [], True
        for a in _actions_for(plan, order.item_ids):
            try:
                lines.append(vendors.apply(order.trip_id, a, payment_id if a.cost_usd > 0 else None))
            except Exception as e:  # report per item; coordinator decides what happens next
                ok = False
                lines.append(f"{a.op} {a.id} failed: {e}")
        return VendorResult(trip_id=order.trip_id, version=order.version, vendor=kind,
                            ok=ok, lines=lines, payment_id=payment_id)

    @book_proto.on_message(VendorOrder)
    async def on_order(ctx: Context, sender: str, order: VendorOrder):
        plan = _approved_plan(order.trip_id, order.version)
        if plan is None or plan.plan_hash != order.plan_hash:
            await ctx.send(sender, VendorResult(trip_id=order.trip_id, version=order.version, vendor=kind,
                                                ok=False, lines=["refused: plan not approved"]))
            return
        actions = _actions_for(plan, order.item_ids)
        problems = [f"{a.op} {a.id} failed: {why}" for a in actions if (why := vendors.precheck(order.trip_id, a))]
        if problems:  # refuse before any money moves
            await ctx.send(sender, VendorResult(trip_id=order.trip_id, version=order.version, vendor=kind,
                                                ok=False, lines=problems))
            return
        payable = [a for a in actions if a.op in ("book", "modify") and a.cost_usd > 0]
        amount = round(sum(a.cost_usd for a in payable), 2)
        if amount <= 0:   # cancels and free changes need no money
            await ctx.send(sender, _finish(order, None))
            return
        ref = f"{order.trip_id}:v{order.version}:{kind}:{order.op}"
        orders[ref] = order
        ctx.logger.info(f"requesting ${amount:,.2f} from locker room for {[a.id for a in payable]}")
        await ctx.send(locker_address_ref["address"], RequestPayment(
            accepted_funds=[Funds(amount=f"{amount:.2f}", currency="USD", payment_method="stripe_test")],
            recipient=agent.address, deadline_seconds=300, reference=ref,
            description=f"TripCheck {kind}: {', '.join(f'{a.op} {a.id}' for a in payable)}",
            metadata={"trip_id": order.trip_id, "version": str(order.version), "plan_hash": order.plan_hash,
                      "vendor": kind, "items": ",".join(a.id for a in payable)}))

    @pay_proto.on_message(CommitPayment)
    async def on_commit(ctx: Context, sender: str, msg: CommitPayment):
        order = orders.pop(msg.reference or "", None)
        if order is None:
            await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason="unknown order"))
            return
        result = _finish(order, msg.transaction_id)
        if result.ok:
            await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
        else:  # couldn't deliver what was paid for: void the payment
            await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason="; ".join(result.lines)))
        await ctx.send(coordinator_ref["address"], result)

    @pay_proto.on_message(RejectPayment)
    async def on_reject(ctx: Context, sender: str, msg: RejectPayment):
        # RejectPayment has no reference; a vendor has at most one order waiting at a time.
        for ref, order in list(orders.items()):
            orders.pop(ref)
            await ctx.send(coordinator_ref["address"], VendorResult(
                trip_id=order.trip_id, version=order.version, vendor=kind, ok=False,
                lines=[f"locker room refused payment: {msg.reason}"]))

    agent.include(book_proto)
    agent.include(pay_proto, publish_manifest=True)
    return agent


# ---------------------------------------------------------------- coordinator
def _kind(item_id: str) -> str | None:
    return next((k for k, p in PREFIX.items() if item_id.startswith(p)), None)


def make_coordinator(port: int, vendor_addresses: dict[str, str]) -> Agent:
    """Runs a plan in two phases: (1) books + changes, (2) cancels, and only if
    phase 1 fully succeeded. A failed reschedule never loses the old booking."""
    agent = Agent(name="tripcheck-booking-coordinator", **_kwargs("coordinator", port))
    jobs: dict[str, dict] = {}

    async def _start_phase(ctx: Context, key: str):
        job = jobs[key]
        label, groups = job["phases"][job["phase"]]
        job["waiting"] = len(groups)
        for kind, ids in groups.items():
            await ctx.send(vendor_addresses[kind], VendorOrder(
                trip_id=job["trip_id"], version=job["version"], plan_hash=job["plan_hash"],
                op=label, item_ids=ids))

    async def _finish(ctx: Context, key: str, note: str = ""):
        job = jobs.pop(key)
        ok = all(r.ok for r in job["results"]) and not note
        lines = [f"{'✅ Done' if ok else '⚠️ Not fully done'} · trip {job['trip_id']} (plan v{job['version']})"]
        for r in job["results"]:
            lines += [f"- {r.vendor}: {line}" for line in r.lines]
        if note:
            lines.append(note)
        paid = [r.payment_id for r in job["results"] if r.payment_id and r.ok]
        if paid:
            lines.append(f"Paid through the locker room: {', '.join(paid)}")
        await ctx.send(job["reply_to"], PlanExecuted(trip_id=job["trip_id"], version=job["version"],
                                                     ok=ok, summary="\n".join(lines)))

    @BOOKING.on_message(ExecutePlan)
    async def on_execute(ctx: Context, sender: str, msg: ExecutePlan):
        plan = _approved_plan(msg.trip_id, msg.version)
        if plan is None or plan.plan_hash != msg.plan_hash:
            await ctx.send(sender, PlanExecuted(trip_id=msg.trip_id, version=msg.version, ok=False,
                                                summary="Refused: plan is not approved or has changed."))
            return
        phase_list = []
        for label, ops in (("book", ("book", "modify")), ("cancel", ("cancel",))):
            groups: dict[str, list[str]] = {}
            for a in plan.actions:
                if a.op in ops and _kind(a.id):
                    groups.setdefault(_kind(a.id), []).append(a.id)
            if groups:
                phase_list.append((label, groups))
        key = f"{msg.trip_id}:v{msg.version}"
        jobs[key] = {"reply_to": sender, "trip_id": msg.trip_id, "version": msg.version,
                     "plan_hash": msg.plan_hash, "phases": phase_list, "phase": 0, "results": []}
        ctx.logger.info(f"executing {key}: phases {[p[0] for p in phase_list]}")
        if not phase_list:
            await _finish(ctx, key)
            return
        await _start_phase(ctx, key)

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
        phase_ok = all(r.ok for r in job["results"])
        job["phase"] += 1
        if job["phase"] < len(job["phases"]):
            if not phase_ok:
                await _finish(ctx, key, "Old bookings were kept, because the new ones didn't all go through.")
                return
            await _start_phase(ctx, key)
            return
        await _finish(ctx, key)

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


DEFAULT_SEED = "tripcheck-orchestrator-dev-seed"


def main():
    if (config.AGENTS_USE_MAILBOX or config.BOOKING_AGENTS_MAILBOX) and config.AGENT_SEED_PHRASE == DEFAULT_SEED:
        raise SystemExit("Set AGENT_SEED_PHRASE in .env to a long random secret before registering on "
                         "Agentverse. The default seed is public, so anyone could impersonate these agents.")
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
