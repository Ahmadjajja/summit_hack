"""Orchestrator uAgent for ASI:One (owner: P1), wired to the Fetch.ai booking agents.

Chat flow (Agent Chat Protocol):
  user message      → plan_trip(): intake → planner ⇄ verifier (≤3 loops) → gate message
  "approve"         → gate.approve() → ExecutePlan → booking coordinator
                      (vendors ⇄ locker room over the Agent Payment Protocol)
  PlanExecuted      → confirmation posted back into the same chat

Each chat sender has one trip; follow-ups ("cancel my Osaka hotel") go to the
same trip. Session state lives in MongoDB (`sessions`), so restarts are safe.

Run everything in one process (orchestrator with a mailbox for ASI:One + the
five booking agents):
    python -m orchestrator.agent
"""
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from uagents import Agent, Bureau, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

from common import config
from common.db import get_db, plans_col
from gate import gate
from orchestrator.pipeline import plan_trip
from vendors.protocol import ExecutePlan, PlanExecuted


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

    @chat.on_message(ChatMessage)
    async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc),
                                                   acknowledged_msg_id=msg.msg_id))
        text = " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
        if not text:
            return
        s = _sessions().find_one({"sender": sender}) or {}
        trip_id = s.get("trip_id") or f"T-{uuid4().hex[:6].upper()}"

        if s.get("awaiting_version") and gate.APPROVE_RE.search(text):
            version = s["awaiting_version"]
            sig = await asyncio.to_thread(gate.approve, trip_id, version, text)
            if sig is None:
                await ctx.send(sender, _reply("That plan can't be approved. Tell me what to change."))
                return
            plan_hash = plans_col().find_one({"trip_id": trip_id, "version": version})["plan_hash"]
            _sessions().update_one({"sender": sender}, {"$set": {"awaiting_version": None,
                                                                 "executing": f"{trip_id}:v{version}"}})
            await ctx.send(coordinator_address, ExecutePlan(trip_id=trip_id, version=version, plan_hash=plan_hash))
            await ctx.send(sender, _reply("Approved. Booking it now…"))
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
