"""The loop (owner: P1). Wires the steps together through signals.

  intake → ⚡ready → planner v1 → ⚡check → verifier
     PASS         → gate (skip remaining loops)
     REJECT, N<3  → ⚡replan v(N+1) → planner …
     REJECT, N=3  → ⚡max_loops → gate shows best version
  gate "approve" → ⚡pay (if new money) → locker → ⚡execute → vendors → reply
"""
from common.contracts import Signal
from common.signals import dispatch, log, on
from gate import gate
from intake.intake import run_intake
from locker.locker import run_locker
from planner.planner import run_planner
from vendors.vendors import run_execute
from verifier.verifier import run_verifier


@on("ready")
def _ready(s: Signal):
    return run_planner(Signal(type="replan", trip_id=s.trip_id, version=1))


@on("replan")
def _replan(s: Signal):
    return run_planner(s)


@on("check")
def _check(s: Signal):
    return run_verifier(s)


@on("pay")
def _pay(s: Signal):
    return run_locker(s)


@on("execute")
def _execute(s: Signal):
    return run_execute(s)


def plan_trip(trip_id: str, raw_text: str, trace=print) -> tuple[str, int]:
    """Run intake + the verification loop. Returns (chat message, version shown)."""
    sig = run_intake(trip_id, raw_text)
    trace(log(sig))
    while sig.type not in ("pass", "max_loops"):
        sig = dispatch(sig)
        trace(log(sig))
    return gate.build_message(sig)


def approve_and_execute(trip_id: str, version: int, user_text: str, trace=print) -> str:
    sig = gate.approve(trip_id, version, user_text)
    if sig is None:
        return "Not approved. Tell me what to change, or reply 'approve'."
    trace(log(sig))
    while sig.type != "execute":
        sig = dispatch(sig)
        trace(log(sig))
    return dispatch(sig)
