import asyncio, os, sys, json, time
os.environ["AIOHTTP_TRUST_ENV"] = "true"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import config  # applies the aiohttp proxy patch
from datetime import datetime, timezone
from uuid import uuid4
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent, MetadataContent, chat_protocol_spec
STATE = {}
from uagents_core.contrib.protocols.payment import RequestPayment, payment_protocol_spec, RejectPayment, CompletePayment, CancelPayment
from uagents.mailbox import AgentverseConnectRequest, RegistrationRequest, register_in_agentverse

FLIGHTS = "agent1qgj8z70l5qstn0cq5lnn2q2mpjcpq4hu2rrm446lxrszchq0zv93ygq4sar"
MAPS = "agent1qgjsj6ppe5armd560jxzrd6pck8kcfl87uw9kqyn2hn2mlkmxjzvz5duelg"
ASKS_ALL = {FLIGHTS: "Find the cheapest one-way economy flights from SFO (San Francisco) to LAX (Los Angeles) on 2026-11-15 for 1 adult. List airline, flight number, departure time and price in USD.",
        MAPS: "Find Desi (Pakistani or Indian) halal restaurants near Hollywood, Los Angeles. Give name, address and rating for up to 5."}
ASKS = {FLIGHTS: ASKS_ALL[FLIGHTS]}
agent = Agent(name="tripcheck-probe", seed=config.AGENT_SEED_PHRASE + "-probe", port=8111, mailbox=True)
chat = Protocol(spec=chat_protocol_spec)
pay = Protocol(spec=payment_protocol_spec, role="buyer")
start = time.time()

@chat.on_message(ChatMessage)
async def got(ctx: Context, sender: str, msg: ChatMessage):
  try:
    await _got(ctx, sender, msg)
  except Exception:
    import traceback; traceback.print_exc(); sys.stdout.flush()

async def _got(ctx, sender, msg):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    who = "FLIGHTS" if sender == FLIGHTS else "MAPS" if sender == MAPS else sender[:16]
    for c in msg.content:
        print(f"\n<<< {who} after {time.time()-start:.0f}s [{type(c).__name__}]\n{getattr(c,'text',c)}", flush=True)

@chat.on_message(ChatAcknowledgement)
async def ack(ctx, sender, msg): print(f"ack from {sender[:16]}", flush=True)

@pay.on_message(RequestPayment)
async def req(ctx, sender, msg: RequestPayment):
    print(f"\n<<< PAYMENT REQUEST from {sender[:16]}: {msg.model_dump_json()[:800]}", flush=True)
    await ctx.send(sender, RejectPayment(reason="probe only, not buying"))

@agent.on_event("startup")
async def go(ctx: Context):
    req_ = AgentverseConnectRequest(user_token=config.AGENTVERSE_API_KEY, agent_type="mailbox")
    det = RegistrationRequest(address=agent.address, name=agent.name, profile=agent._build_registration_profile(),
                              endpoints=agent._endpoints, protocols=list(agent.protocols.keys()), metadata=agent.metadata)
    r = await register_in_agentverse(req_, agent._identity, agent._prefix, agent._agentverse, det)
    print("registered:", r.success, r.detail, flush=True)
    await asyncio.sleep(8)
    for addr, text in ASKS.items():
        await ctx.send(addr, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                         content=[TextContent(type="text", text=text)]))
        print(">>> sent to", "FLIGHTS" if addr == FLIGHTS else "MAPS", flush=True)

@pay.on_message(CompletePayment)
async def cp(ctx, sender, msg): print("complete", msg, flush=True)

@pay.on_message(CancelPayment)
async def cc(ctx, sender, msg): print("cancel", msg, flush=True)

agent.include(chat); agent.include(pay)
agent.run()
