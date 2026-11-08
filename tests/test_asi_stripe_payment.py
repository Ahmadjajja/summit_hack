"""Fetch.ai-style Stripe payment on the ASI:One agent, offline, with real uAgents.

A stand-in ASI:One traveller ("buyer") chats with the orchestrator ("seller"):
  approve → RequestPayment with metadata["stripe"] (embedded Checkout Session)
  CommitPayment before paying → CancelPayment "not completed yet"
  pay with 4242… → CommitPayment → CompletePayment → booked by the Fetch agents
  a change → RejectPayment → session expired, nothing booked
  "pay link" → hosted Checkout → paid without CommitPayment → the poller notices → booked
  cancel → refund to the Checkout payment
Stripe is a fake module with test-mode behaviour (tests/fake_stripe.py).

    python -m tests.test_asi_stripe_payment
"""
import asyncio
import os
import sys

os.environ["MOCK"] = "subq,gemini,asi"
os.environ["DEMO_NOW"] = "2026-11-18T10:00:00"
os.environ["MONGODB_URI"] = ""
os.environ["STRIPE_TEST_KEY"] = "sk_test_fake"
os.environ["STRIPE_PUBLISHABLE_KEY"] = "pk_test_fake"
os.environ["BOOKING_DRY_RUN"] = "false"

from tests import fake_stripe  # noqa: E402

fake_stripe.install()

from datetime import datetime, timezone  # noqa: E402
from uuid import uuid4  # noqa: E402

from uagents import Agent, Bureau, Context, Protocol  # noqa: E402
from uagents_core.contrib.protocols.chat import (  # noqa: E402
    ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec)
from uagents_core.contrib.protocols.payment import (  # noqa: E402
    CancelPayment, CommitPayment, CompletePayment, RejectPayment, RequestPayment, payment_protocol_spec)

from common.db import ledger_col  # noqa: E402
from orchestrator.agent import build_all  # noqa: E402
from seed.seed_japan import seed  # noqa: E402

FAILS = []
INBOX: asyncio.Queue = asyncio.Queue()


def check(cond, what):
    print(f"  {'PASS' if cond else 'FAIL'}  {what}")
    if not cond:
        FAILS.append(what)


def finish():
    print("\nALL PASSED" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    sys.stdout.flush()
    os._exit(1 if FAILS else 0)


async def expect(kind, timeout=60):
    """Next message of this kind (chat text, or a payment message), skipping nothing silently."""
    while True:
        try:
            k, msg = await asyncio.wait_for(INBOX.get(), timeout)
        except asyncio.TimeoutError:
            check(False, f"timed out waiting for {kind}")
            finish()
        if k == kind:
            return msg
        print(f"    (got {k} while waiting for {kind}: {str(msg)[:120]})")
        if k == "chat":
            continue
        check(False, f"expected {kind}, got {k}")
        return None


async def script(ctx: Context, orch: str):
    async def say(text):
        print(f"\nYou: {text}")
        await ctx.send(orch, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                         content=[TextContent(type="text", text=text)]))

    await asyncio.sleep(1)
    await say("Japan Nov 15-22, Tokyo then Osaka for the dev events, budget $3,500")
    check("Reply **approve**" in await expect("chat"), "verified plan in chat")

    await say("approve")
    req = await expect(RequestPayment)
    stripe_meta = (req.metadata or {}).get("stripe", {})
    check(req.accepted_funds[0].payment_method == "stripe" and req.accepted_funds[0].amount == "2910.00",
          f"RequestPayment for $2,910.00 via 'stripe' (got {req.accepted_funds[0].amount})")
    check(stripe_meta.get("ui_mode") == "embedded" and stripe_meta.get("publishable_key") == "pk_test_fake"
          and stripe_meta.get("client_secret") and stripe_meta.get("checkout_session_id", "").startswith("cs_test_"),
          "metadata['stripe'] carries the embedded Checkout Session (Fetch.ai example fields)")
    check("sk_test" not in str(req.metadata), "secret key never sent to the traveller")
    msg = await expect("chat")
    check("4242 4242 4242 4242" in msg and "pay link" in msg, "chat explains the test card and the link fallback")
    check(ledger_col().count_documents({"type": "booking"}) == 0, "nothing booked before paying")
    sid = stripe_meta["checkout_session_id"]

    await ctx.send(orch, CommitPayment(funds=req.accepted_funds[0], recipient=req.recipient, transaction_id=sid))
    cancel = await expect(CancelPayment)
    check(cancel and "not completed" in (cancel.reason or ""), "CommitPayment before paying → CancelPayment")

    fake_stripe.pay(sid)
    await ctx.send(orch, CommitPayment(funds=req.accepted_funds[0], recipient=req.recipient, transaction_id=sid))
    done = await expect(CompletePayment)
    check(done and done.transaction_id == sid, "paid → CompletePayment with the Checkout Session ID")
    check("confirmed $2,910.00" in await expect("chat"), "chat: Stripe confirmed")
    check("✅ Done" in await expect("chat"), "Fetch agents booked it")
    pi = fake_stripe.SESSIONS[sid].payment_intent
    pays = list(ledger_col().find({"type": "payment", "version": {"$exists": True}}))
    check(len(pays) == 3 and all(p["payment_id"].startswith(pi + "#") for p in pays)
          and abs(sum(p["amount_usd"] for p in pays) - 2910) < 0.01,
          "locker room paid the 3 vendors out of the one Stripe payment")

    await say("Stay 2 more nights in Osaka")
    check("You pay now: $490" in await expect("chat"), "extension costs $490")
    await say("approve")
    req2 = await expect(RequestPayment)
    await expect("chat")
    sid2 = req2.metadata["stripe"]["checkout_session_id"]
    await ctx.send(orch, RejectPayment(reason="not now"))
    check("declined" in await expect("chat"), "RejectPayment → nothing booked")
    check(fake_stripe.SESSIONS[sid2].status == "expired", "declined session expired on Stripe")

    await say("pay link")
    req3 = await expect(RequestPayment)
    meta3 = req3.metadata["stripe"]
    check(meta3["ui_mode"] == "hosted" and meta3.get("url", "").startswith("https://checkout.stripe.com/"),
          "'pay link' → hosted Checkout link")
    msg = await expect("chat")
    check("checkout.stripe.com" in msg, "link shown in chat")
    fake_stripe.pay(meta3["checkout_session_id"])     # paid on the page; this client never sends CommitPayment
    check("confirmed $490.00" in await expect("chat", timeout=30), "poller noticed the payment")
    check("✅ Done" in await expect("chat"), "extension applied")

    await say("Cancel my Osaka hotel")
    check("no card needed" in await expect("chat"), "cancel needs no payment")
    await say("approve")
    check("Booking it now" in await expect("chat"), "no RequestPayment for a cancel")
    out = await expect("chat")
    check("cancelled HTL-4471" in out, "Osaka hotel cancelled")
    refunded = {p for p, _ in fake_stripe.REFUNDS}
    check(refunded and refunded <= {pi, fake_stripe.SESSIONS[meta3['checkout_session_id']].payment_intent},
          "refund went back to the Stripe payments for that hotel")
    finish()


def main():
    seed()
    agents = build_all(mailbox=False)
    user = Agent(name="asi-one-traveller", seed="tripcheck-test-asi-traveller")
    chat = Protocol(spec=chat_protocol_spec)
    pay = Protocol(spec=payment_protocol_spec, role="buyer")

    @user.on_event("startup")
    async def start(ctx: Context):
        asyncio.get_event_loop().create_task(script(ctx, agents["orchestrator"].address))

    @chat.on_message(ChatMessage)
    async def got(ctx: Context, sender: str, msg: ChatMessage):
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc),
                                                   acknowledged_msg_id=msg.msg_id))
        text = " ".join(c.text for c in msg.content if isinstance(c, TextContent))
        print(f"  TripCheck: {text.splitlines()[0][:110]}")
        await INBOX.put(("chat", text))

    @chat.on_message(ChatAcknowledgement)
    async def ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
        pass

    for kind in (RequestPayment, CancelPayment, CompletePayment):
        async def handler(ctx: Context, sender: str, msg, kind=kind):
            print(f"  ⇢ {kind.__name__}")
            await INBOX.put((kind, msg))
        pay.on_message(kind)(handler)

    user.include(chat)
    user.include(pay)
    Bureau(agents=list(agents.values()) + [user], port=8097).run()


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
