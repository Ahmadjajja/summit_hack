"""Shared data contracts. Every step reads and writes these shapes.

Change a model here only with the whole team's agreement. See SPEC.md.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Intent = Literal["book", "cancel", "reschedule", "extend"]
SignalType = Literal["ready", "check", "replan", "pass", "max_loops", "pay", "execute"]


# ---- Signals: IDs only, never data, amounts or card details -------------
class Signal(BaseModel):
    type: SignalType
    trip_id: str
    version: Optional[int] = None
    plan_hash: Optional[str] = None
    payment_id: Optional[str] = None


# ---- ① Intake → requests -------------------------------------------------
class TripRequest(BaseModel):
    trip_id: str
    raw_text: str
    intent: Intent
    cities: list[str]
    start: str                      # YYYY-MM-DD
    end: str                        # YYYY-MM-DD
    budget_usd: float
    events: list[str] = []          # event IDs
    prefs: dict = {}                # pace, dining_style, budget_tier ...
    target_booking: Optional[str] = None   # for cancel / reschedule / extend
    intake_confidence: dict = {}    # SubQ confidence per question


# ---- ② Planner → plans ---------------------------------------------------
class SlotPick(BaseModel):
    slot: str                       # flight_out, hotel_tokyo, dinner_tokyo_1116 ...
    pick: str                       # candidate id
    pick_detail: dict
    confidence: float
    probs: dict[str, float] = {}
    candidates: list[dict] = []     # every option for this slot, full detail
    cost_usd: float


class Action(BaseModel):
    """One thing the vendors must do.

    book   id = candidate id         cost_usd = price (> 0)
    modify id = existing booking id  cost_usd = extra to pay (>= 0), params = changes
           hotel:  {"new_check_in": "YYYY-MM-DD", "new_check_out": "YYYY-MM-DD"}
           flight: {"new_date": "YYYY-MM-DD", "new_flight": "FLT-…"}
    cancel id = existing booking id  cost_usd = -expected refund (<= 0)
    Build change actions with vendors/policy.py so prices follow vendor policy.
    """
    op: Literal["book", "cancel", "modify"]
    id: str
    cost_usd: float = 0.0
    params: dict = {}


class PlanPackage(BaseModel):
    trip_id: str
    version: int
    intent: Intent
    slots: list[SlotPick]
    actions: list[Action]
    total_usd: float
    money_delta_usd: float          # > 0 means new money goes out
    excluded_ids: list[str] = []    # rejected in earlier loops
    plan_hash: Optional[str] = None
    approved: bool = False


# ---- ③ Verifier → verdicts ----------------------------------------------
class Check(BaseModel):
    rule: str
    ok: bool
    severity: Literal["hard", "soft"]
    detail: str = ""


class Issue(BaseModel):
    slot: Optional[str] = None
    rule: str
    severity: Literal["hard", "soft"]
    why: str
    found: Optional[str] = None     # suggested replacement candidate id
    saves_usd: float = 0.0


class Verdict(BaseModel):
    trip_id: str
    version: int
    result: Literal["PASS", "REJECT"]
    checks: list[Check]
    issues: list[Issue] = []
    replace: dict[str, str] = {}    # slot -> candidate id
    hard_failures: int = 0
    soft_failures: int = 0


# ---- every step → run_log ------------------------------------------------
class RunLog(BaseModel):
    trip_id: str
    step: Literal["intake", "planner", "verifier", "gate", "locker", "execute"]
    loop: int = 0
    version: Optional[int] = None
    model: str
    started_at: str
    ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    note: str = Field(default="")
