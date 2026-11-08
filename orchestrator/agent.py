"""Orchestrator uAgent for ASI:One (owner: P1), wired to the Fetch.ai booking agents.

Chat flow (Agent Chat Protocol):
  user message      → plan_trip(): intake → planner ⇄ verifier (≤3 loops) → gate message
  "approve"         → gate.approve() → ExecutePlan → booking coordinator
                      (vendors ⇄ locker room over the Agent Payment Protocol)

With Stripe test mode on (STRIPE_TEST_KEY=sk_test_…, BOOKING_DRY_RUN=false), a plan
that needs money is paid by the traveller first, as in Fetch.ai's Stripe example:
  "approve"         → RequestPayment to the traveller, metadata["stripe"] = Checkout Session
                      (embedded card with STRIPE_PUBLISHABLE_KEY, else a hosted link)
  CommitPayment     → Stripe says paid, for this plan and amount? → CompletePayment → ExecutePlan
                      not paid yet → CancelPayment ("please finish checkout"), still waiting
  RejectPayment     → session expired, nothing booked
A 5 s poll also notices payments from clients that never send CommitPayment.
  PlanExecuted      → confirmation posted back into the same chat

Each chat sender has one trip; follow-ups ("cancel my Osaka hotel") go to the
same trip. Session state lives in MongoDB (`sessions`), so restarts are safe.

Run everything in one process (orchestrator with a mailbox for ASI:One + the
five booking agents):
    python -m orchestrator.agent
"""
import asyncio
import re
from datetime import datetime, timezone
from uuid import uuid4

from uagents import Agent, Bureau, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)
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
from common.db import get_db, plans_col
from gate import gate
from locker import locker
from orchestrator import chat_text
from orchestrator.pipeline import plan_trip
from vendors.protocol import ExecutePlan, PlanExecuted


PAY_LINK_RE = re.compile(r"\b(pay(ment)?\s+link|link)\b", re.I)
PAID_RE = re.compile(r"^\s*(paid|i paid|done|pay|retry|check)\b", re.I)


def _sessions():
    return get_db()["sessions"]


def _reply(text: str) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                       content=[TextContent(type="text", text=text)])


def make_orchestrator(coordinator_address: str, mailbox: bool | None = None) -> Agent:
    use_mailbox = config.AGENTS_USE_MAILBOX if mailbox is None else mailbox
    kw = {"port": 8000, "mailbox": True} if use_mailbox else {}
    agent = Agent(name="tripcheck", seed=config.AGENT_SEED_PHRASE,
                  description="TripCheck: plans your trip, an independent AI verifies it, you approve, "
                              "then Fetch.ai agents book. Handles cancel, reschedule and extend mid-trip.",
                  **kw)
    chat = Protocol(spec=chat_protocol_spec)
    booking = Protocol(name="TripCheckBookingClient", version="0.1.0")
    # We are the "seller" towards the traveller: RequestPayment out, CommitPayment / RejectPayment in
    payment = Protocol(spec=payment_protocol_spec, role="seller")

    async def _book(ctx: Context, sender: str, trip_id: str, version: int, note: str):
        plan_hash = plans_col().find_one({"trip_id": trip_id, "version": version})["plan_hash"]
        _sessions().update_one({"sender": sender}, {"$set": {"executing": f"{trip_id}:v{version}"}})
        await ctx.send(coordinator_address, ExecutePlan(trip_id=trip_id, version=version, plan_hash=plan_hash))
        await ctx.send(sender, _reply(note))

    async def _request_payment(ctx: Context, sender: str, trip_id: str, version: int, hosted: bool = False):
        """Send the traveller a Fetch.ai RequestPayment with a Stripe Checkout Session in metadata["stripe"]
        (embedded card when the publishable key is set, as in Fetch.ai's Stripe example; else a link)."""
        embedded = bool(config.STRIPE_PUBLISHABLE_KEY) and not hosted
        try:
            co = await asyncio.to_thread(locker.create_checkout, trip_id, version, config.STRIPE_SUCCESS_URL,
                                         "embedded" if embedded else "hosted")
        except locker.LockerRefused as e:
            pending = (_sessions().find_one({"sender": sender}) or {}).get("awaiting_payment")
            if pending and await _try_finish_payment(ctx, _sessions().find_one({"sender": sender}), pending):
                return
            await ctx.send(sender, _reply(f"I couldn't start the payment: {e}"))
            return
        cents = str(int(round(co["amount_usd"] * 100)))
        stripe_meta = {"ui_mode": co["ui_mode"], "checkout_session_id": co["session_id"],
                       "currency": "usd", "amount_cents": cents}
        if embedded:
            stripe_meta.update(publishable_key=config.STRIPE_PUBLISHABLE_KEY, client_secret=co["client_secret"])
        else:
            stripe_meta["url"] = co["url"]
        _sessions().update_one({"sender": sender}, {"$set": {"awaiting_payment": {
            "trip_id": trip_id, "version": version, "session_id": co["session_id"],
            "ui_mode": co["ui_mode"], "amount_usd": co["amount_usd"]}}})
        await ctx.send(sender, RequestPayment(
            accepted_funds=[Funds(currency="USD", amount=f"{co['amount_usd']:.2f}", payment_method="stripe")],
            recipient=str(ctx.agent.address), deadline_seconds=30 * 60, reference=f"{trip_id}:v{version}",
            description=f"TripCheck trip {trip_id} · plan v{version}",
            metadata={"stripe": stripe_meta, "service": "tripcheck_booking",
                      "trip_id": trip_id, "version": str(version)}))
        how = ("on the Stripe card in this chat" if embedded else f"here: [Stripe Checkout]({co['url']})")
        await ctx.send(sender, _reply(
            f"Approved. Pay **${co['amount_usd']:,.2f}** {how}. Stripe test mode, no real money: card "
            "`4242 4242 4242 4242`, any future date, any CVC. I book as soon as Stripe confirms."
            + ("\n\nNo card showing? Reply **pay link** for a Stripe page instead." if embedded else
               "\n\nAfter paying, reply **paid** if I haven't confirmed within a few seconds.")))

    async def _try_finish_payment(ctx: Context, s: dict, pending: dict, commit_sender: str | None = None) -> bool:
        """If Stripe says this session is paid (for this plan and amount): book it. Exactly once."""
        try:
            await asyncio.to_thread(locker.confirm_checkout, pending["session_id"])
        except locker.LockerRefused:
            return False
        claimed = _sessions().find_one_and_update(
            {"_id": s["_id"], "awaiting_payment.session_id": pending["session_id"]},
            {"$set": {"awaiting_payment": None, "paid_session": pending["session_id"]}})
        if not claimed:
            return True        # someone else (CommitPayment vs. the poller) already started the booking
        if commit_sender:
            await ctx.send(commit_sender, CompletePayment(transaction_id=pending["session_id"]))
        await _book(ctx, s["sender"], pending["trip_id"], pending["version"],
                    f"💳 Stripe (test mode) confirmed ${pending['amount_usd']:,.2f}. Booking it now…")
        return True

    @payment.on_message(CommitPayment)
    async def on_commit(ctx: Context, sender: str, msg: CommitPayment):
        s = _sessions().find_one({"sender": sender}) or {}
        pending = s.get("awaiting_payment")
        if not pending:
            if msg.transaction_id and msg.transaction_id == s.get("paid_session"):
                await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))   # already booked
            else:
                await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id,
                                                     reason="No payment is pending for this chat."))
            return
        if msg.funds.payment_method != "stripe" or msg.transaction_id != pending["session_id"]:
            await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id,
                                                 reason="Pay with the Stripe checkout from the latest request."))
            return
        if not await _try_finish_payment(ctx, s, pending, commit_sender=sender):
            await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id,
                                                 reason="Stripe payment not completed yet. Please finish checkout."))

    @payment.on_message(RejectPayment)
    async def on_reject(ctx: Context, sender: str, msg: RejectPayment):
        s = _sessions().find_one({"sender": sender}) or {}
        pending = s.get("awaiting_payment")
        if not pending:
            return
        await asyncio.to_thread(locker.expire_checkout, pending["session_id"])
        await ctx.send(sender, _reply("Payment declined, so nothing was booked or charged. Reply **pay** to try "
                                      "again, or tell me what to change."))

    @agent.on_interval(period=5.0)
    async def poll_payments(ctx: Context):
        """Chat clients that don't send CommitPayment (or a hosted link): notice the payment ourselves."""
        if not locker.checkout_enabled():
            return
        for s in _sessions().find({"awaiting_payment": {"$ne": None}}):
            pending = s.get("awaiting_payment")
            if pending:
                await _try_finish_payment(ctx, s, pending)

    @chat.on_message(ChatMessage)
    async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc),
                                                   acknowledged_msg_id=msg.msg_id))
        text = chat_text.clean(" ".join(c.text for c in msg.content if isinstance(c, TextContent)))
        if not text:
            return
        if chat_text.is_help(text):
            await ctx.send(sender, _reply(chat_text.HELP))
            return
        s = _sessions().find_one({"sender": sender}) or {}
        trip_id = s.get("trip_id") or f"T-{uuid4().hex[:6].upper()}"

        pending = s.get("awaiting_payment")
        if pending:   # approved, waiting for the traveller to pay on Stripe
            if PAY_LINK_RE.search(text):
                await _request_payment(ctx, sender, pending["trip_id"], pending["version"], hosted=True)
            elif PAID_RE.search(text) or gate.APPROVE_RE.search(text):
                if not await _try_finish_payment(ctx, s, pending):
                    await _request_payment(ctx, sender, pending["trip_id"], pending["version"],
                                           hosted=pending.get("ui_mode") == "hosted")
            else:
                await ctx.send(sender, _reply(
                    f"Your approved plan is waiting for payment (${pending['amount_usd']:,.2f}). Pay on the Stripe "
                    "card, or reply **pay link** for a Stripe page. Nothing is booked until Stripe confirms."))
            return

        if s.get("awaiting_version") and gate.APPROVE_RE.search(text):
            version = s["awaiting_version"]
            sig = await asyncio.to_thread(gate.approve, trip_id, version, text)
            if sig is None:
                await ctx.send(sender, _reply("That plan can't be approved. Tell me what to change."))
                return
            _sessions().update_one({"sender": sender}, {"$set": {"awaiting_version": None}})
            if sig.type == "pay" and locker.checkout_enabled():
                # Fetch.ai Agent Payment Protocol: ask the traveller to pay with Stripe (test mode) first
                await _request_payment(ctx, sender, trip_id, version)
                return
            await _book(ctx, sender, trip_id, version, "Approved. Booking it now…")
            return

        try:
            out, version = await asyncio.to_thread(plan_trip, trip_id, text, ctx.logger.info)
        except Exception as e:  # never leave the user without an answer
            ctx.logger.exception("planning failed")
            await ctx.send(sender, _reply(f"Sorry, something went wrong while planning ({type(e).__name__}). "
                                          "Please try again or rephrase."))
            return
        _sessions().update_one({"sender": sender},
                               {"$set": {"trip_id": trip_id, "awaiting_version": version}}, upsert=True)
        await ctx.send(sender, _reply(out))

    @chat.on_message(ChatAcknowledgement)
    async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
        pass

    @booking.on_message(PlanExecuted)
    async def on_done(ctx: Context, sender: str, msg: PlanExecuted):
        s = _sessions().find_one({"executing": f"{msg.trip_id}:v{msg.version}"})
        if not s:
            return
        _sessions().update_one({"_id": s["_id"]}, {"$set": {"executing": None}})
        await ctx.send(s["sender"], _reply(msg.summary))

    if use_mailbox and config.AGENTVERSE_API_KEY:
        @agent.on_event("startup")
        async def connect_mailbox(ctx: Context):
            """Register on Agentverse with the API key (what the inspector's Connect button does)."""
            from uagents.mailbox import AgentverseConnectRequest, RegistrationRequest, register_in_agentverse
            req = AgentverseConnectRequest(user_token=config.AGENTVERSE_API_KEY, agent_type="mailbox")
            details = RegistrationRequest(
                address=agent.address, name=agent.name, handle=agent._handle,
                profile=agent._build_registration_profile(), endpoints=agent._endpoints,
                protocols=list(agent.protocols.keys()), metadata=agent.metadata)
            res = await register_in_agentverse(req, agent._identity, agent._prefix, agent._agentverse, details)
            ctx.logger.info(f"Agentverse mailbox: {'connected' if res.success else 'FAILED: ' + str(res.detail)}")
            if res.success:
                ctx.logger.info(f"Chat in ASI:One: https://asi1.ai/ai/{agent.address}")
                ctx.logger.info(f"Agent page:      https://agentverse.ai/agents/details/{agent.address}/profile")

    agent.include(chat, publish_manifest=True)
    agent.include(payment, publish_manifest=True)
    agent.include(booking)
    return agent


def build_all(mailbox: bool | None = None) -> dict[str, Agent]:
    from vendors.agents import build_agents
    agents = build_agents()
    agents["orchestrator"] = make_orchestrator(agents["coordinator"].address, mailbox)
    return agents


def main():
    from vendors.agents import DEFAULT_SEED
    if config.AGENTS_USE_MAILBOX and config.AGENT_SEED_PHRASE == DEFAULT_SEED:
        raise SystemExit("Set AGENT_SEED_PHRASE in .env to a long random secret before going on Agentverse.")
    from seed.seed_japan import seed
    seed(reset=not config.MONGODB_URI)
    agents = build_all()
    print("Orchestrator (chat with this on Agentverse):", agents["orchestrator"].address)
    Bureau(agents=list(agents.values()), port=8099).run()


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
