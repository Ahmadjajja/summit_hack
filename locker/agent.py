"""⑤ Locker room as a Fetch.ai uAgent (owner: P3).

Role "buyer" in Fetch.ai's Agent Payment Protocol: vendor agents send it
`RequestPayment`; it answers `CommitPayment` only if `authorize_and_charge`
agrees the request matches the approved plan in MongoDB, else `RejectPayment`.
"""
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    RejectPayment,
    RequestPayment,
    payment_protocol_spec,
)

from locker.locker import LockerRefused, authorize_and_charge, mark_payment


def make_locker(agent_kwargs: dict, vendor_addresses: dict[str, str]) -> Agent:
    """vendor_addresses: {"flight": addr, "hotel": addr, "dining": addr}.
    Only these agents may ask for money, and only for their own items."""
    agent = Agent(name="tripcheck-locker-room", **agent_kwargs)
    vendor_by_address = {a: v for v, a in vendor_addresses.items()}
    proto = Protocol(spec=payment_protocol_spec, role="buyer")

    @proto.on_message(RequestPayment)
    async def on_request(ctx: Context, sender: str, msg: RequestPayment):
        vendor = vendor_by_address.get(sender)
        meta = msg.metadata or {}
        try:
            if vendor is None:
                raise LockerRefused("unknown vendor agent")
            if meta.get("vendor") != vendor:
                raise LockerRefused("vendor in request does not match sender")
            funds = msg.accepted_funds[0]
            if funds.currency.upper() != "USD":
                raise LockerRefused(f"unsupported currency {funds.currency}")
            rec = authorize_and_charge(
                trip_id=str(meta["trip_id"]), version=int(meta["version"]),
                claimed_hash=str(meta["plan_hash"]), vendor=vendor,
                item_ids=[i for i in str(meta["items"]).split(",") if i],
                amount_usd=float(funds.amount), reference=msg.reference or "",
            )
        except (LockerRefused, KeyError, ValueError, IndexError) as e:
            ctx.logger.warning(f"rejected payment from {vendor or sender[:16]}: {e}")
            await ctx.send(sender, RejectPayment(reason=str(e)))
            return
        ctx.logger.info(f"committed ${rec['amount_usd']:,.2f} to {vendor} ({rec['payment_id']})")
        await ctx.send(sender, CommitPayment(
            funds=msg.accepted_funds[0], recipient=msg.recipient,
            transaction_id=rec["payment_id"], reference=msg.reference,
            description=msg.description))

    @proto.on_message(CompletePayment)
    async def on_complete(ctx: Context, sender: str, msg: CompletePayment):
        if msg.transaction_id:
            mark_payment(msg.transaction_id, "completed")

    @proto.on_message(CancelPayment)
    async def on_cancel(ctx: Context, sender: str, msg: CancelPayment):
        # Vendor couldn't book after we paid: void it. TODO(P3): Stripe refund when not dry run.
        if msg.transaction_id:
            mark_payment(msg.transaction_id, "cancelled")
        ctx.logger.warning(f"payment {msg.transaction_id} cancelled: {msg.reason}")

    agent.include(proto, publish_manifest=True)
    return agent
