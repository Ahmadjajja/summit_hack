"""Skeleton: vendor agents as uAgents (owner: P3). Not wired in yet.

Run:  python -m vendors.agents
VERIFY the uagents API against the current docs before relying on it.
"""
from uagents import Agent, Bureau, Context, Model

from vendors import vendors


class BookRequest(Model):
    trip_id: str
    item_id: str
    cost_usd: float


class BookResult(Model):
    trip_id: str
    item_id: str
    confirmation: str


def make_vendor(name: str, port: int) -> Agent:
    agent = Agent(name=name, port=port, seed=f"tripcheck-{name}-dev-seed")

    @agent.on_message(model=BookRequest, replies=BookResult)
    async def handle(ctx: Context, sender: str, msg: BookRequest):
        rec = vendors.book(msg.trip_id, msg.item_id, msg.cost_usd)
        await ctx.send(sender, BookResult(trip_id=msg.trip_id, item_id=msg.item_id,
                                          confirmation=rec["confirmation"]))

    return agent


if __name__ == "__main__":
    bureau = Bureau()
    for i, n in enumerate(["flight-agent", "hotel-agent", "dining-agent"]):
        a = make_vendor(n, 8101 + i)
        print(n, a.address)
        bureau.add(a)
    bureau.run()
