"""Messages for the booking side (owner: P3).

P1's orchestrator only needs two of them:
    send  ExecutePlan  → BOOKING_COORDINATOR address
    get   PlanExecuted ← back to the orchestrator

Payments between vendor agents and the locker room use Fetch.ai's standard
Agent Payment Protocol (uagents_core.contrib.protocols.payment):
    vendor ──RequestPayment──▶ locker room
    vendor ◀─CommitPayment─── locker room   (or RejectPayment)
    vendor ──CompletePayment─▶ locker room  (after the booking succeeds)
"""
from uagents import Model


class ExecutePlan(Model):
    """Orchestrator → coordinator: carry out an APPROVED plan. IDs only."""
    trip_id: str
    version: int
    plan_hash: str


class PlanExecuted(Model):
    """Coordinator → orchestrator: everything is done (or what failed)."""
    trip_id: str
    version: int
    ok: bool
    summary: str                    # markdown, ready to post in ASI:One chat


class VendorOrder(Model):
    """Coordinator → one vendor agent."""
    trip_id: str
    version: int
    plan_hash: str
    op: str                         # "book" | "cancel"
    item_ids: list[str]


class VendorResult(Model):
    """Vendor agent → coordinator."""
    trip_id: str
    version: int
    vendor: str
    ok: bool
    lines: list[str]                # human-readable result lines
    payment_id: str | None = None
