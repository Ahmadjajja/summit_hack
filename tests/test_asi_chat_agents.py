"""Full wiring test with real uAgents, offline: a stand-in ASI:One user talks to
the orchestrator over the Agent Chat Protocol; approval goes to the Fetch.ai
booking coordinator → vendors ⇄ locker room (Agent Payment Protocol) → back to chat.

    python -m tests.test_asi_chat_agents
"""
import asyncio
import os

os.environ.setdefault("MOCK", "subq,gemini")
os.environ["DEMO_NOW"] = "2026-11-18T10:00:00"
os.environ["MONGODB_URI"] = ""

from datetime import datetime, timezone  # noqa: E402
from uuid import uuid4  # noqa: E402

from uagents import Agent, Bureau, Context, Protocol  # noqa: E402
from uagents_core.contrib.protocols.chat import (  # noqa: E402
    ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec)

from common.db import ledger_col  # noqa: E402
from orchestrator.agent import build_all  # noqa: E402
from seed.seed_japan import seed  # noqa: E402

SCRIPT = [  # (what the user types, text that must appear in the reply)
    ("Japan Nov 15-22, Tokyo then Osaka for the dev events, budget $3,500", "Reply **approve**"),
    ("approve", "Approved. Booking it now"),
    (None, "✅ Done"),
    ("Stay 2 more nights in Osaka", "You pay now: $490"),
    ("approve", "Approved. Booking it now"),
    (None, "✅ Done"),
    ("Cancel my Osaka hotel", "no card needed"),
    ("approve", "Approved. Booking it now"),
    (None, "cancelled HTL-4471 · refund $640"),
]
FAILS = []


def main():
    seed()
    agents = build_all(mailbox=False)
    user = Agent(name="asi-one-user", seed="tripcheck-test-asi-user")
    proto = Protocol(spec=chat_protocol_spec)
    state = {"i": 0}

    async def send_next(ctx):
        while state["i"] < len(SCRIPT) and SCRIPT[state["i"]][0] is None:
            return  # wait for an unprompted reply (booking result)
        if state["i"] >= len(SCRIPT):
            active = ledger_col().count_documents({"type": "booking", "status": "active"})
            paid = sum(p["amount_usd"] for p in ledger_col().find({"type": "payment", "status": "completed"}))
            ok = active == 4 and paid == 3400
            print(f"\nledger: {active} active bookings, ${paid:,.0f} paid → {'PASS' if ok else 'FAIL'}")
            print("\nALL PASSED" if ok and not FAILS else f"\nFAILED: {FAILS}")
            os._exit(0 if ok and not FAILS else 1)
        text = SCRIPT[state["i"]][0]
        print(f"\nYou: {text}")
        await ctx.send(agents["orchestrator"].address, ChatMessage(
            timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[TextContent(type="text", text=text)]))

    @user.on_event("startup")
    async def start(ctx: Context):
        await asyncio.sleep(1)
        await send_next(ctx)

    @proto.on_message(ChatMessage)
    async def got(ctx: Context, sender: str, msg: ChatMessage):
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc),
                                                   acknowledged_msg_id=msg.msg_id))
        reply = " ".join(c.text for c in msg.content if isinstance(c, TextContent))
        expect = SCRIPT[state["i"]][1]
        ok = expect in reply
        print(f"  TripCheck: {reply.splitlines()[0]}  [{'PASS' if ok else 'FAIL'}: expects '{expect}']")
        if not ok:
            FAILS.append(expect)
            print(reply)
        state["i"] += 1
        await send_next(ctx)

    @proto.on_message(ChatAcknowledgement)
    async def ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
        pass

    user.include(proto)
    Bureau(agents=list(agents.values()) + [user], port=8098).run()


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
