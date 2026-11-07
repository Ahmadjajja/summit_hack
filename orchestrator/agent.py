"""Orchestrator uAgent for ASI:One (owner: P1).

Speaks the Agent Chat Protocol with a mailbox, so "Chat with Agent" on
Agentverse works without a public URL. New message → plan_trip(); "approve" →
approve_and_execute(). VERIFY imports against the current Agentverse docs:
https://docs.agentverse.ai/documentation/getting-started/enable-chat-protocol

Run:  python -m orchestrator.agent   (then open the agent on Agentverse)
"""
from datetime import datetime, timezone
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

from common import config
from orchestrator.pipeline import approve_and_execute, plan_trip
from seed.seed_japan import seed

agent = Agent(name="tripcheck", seed=config.AGENT_SEED_PHRASE, port=8000, mailbox=True)
chat = Protocol(spec=chat_protocol_spec)


def _reply(text: str) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                       content=[TextContent(type="text", text=text)])


@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc),
                                               acknowledged_msg_id=msg.msg_id))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    if not text:
        return
    pending = ctx.storage.get(f"pending:{sender}")  # {"trip_id":..., "version":...}
    if pending and "approve" in text.lower():
        out = approve_and_execute(pending["trip_id"], pending["version"], text, trace=ctx.logger.info)
        ctx.storage.set(f"pending:{sender}", None)
    else:
        trip_id = (pending or {}).get("trip_id") or f"T-{uuid4().hex[:6].upper()}"
        out, version = plan_trip(trip_id, text, trace=ctx.logger.info)
        ctx.storage.set(f"pending:{sender}", {"trip_id": trip_id, "version": version})
    await ctx.send(sender, _reply(out))


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(chat, publish_manifest=True)

if __name__ == "__main__":
    seed()
    print("Orchestrator address:", agent.address)
    agent.run()
