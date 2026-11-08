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
from scout.scout import ensure_candidates
from vendors.vendors import run_execute
from verifier.verifier import run_verifier


@on("ready")
def _ready(s: Signal):
    return run_planner(Signal(type="replan", trip_id=s.trip_id, version=s.version or 1))


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


def no_options_message(trip_id: str, found: dict) -> str:
    from common.db import requests_col
    req = requests_col().find_one({"trip_id": trip_id}) or {}
    lines = [f"**Trip {trip_id}**: {' → '.join(req.get('cities', []))}, {req.get('start')} to {req.get('end')}", "",
             "I searched the web with Fetch.ai ASI:One but couldn't find bookable options for:"]
    lines += [f"- {m}" for m in found["missing"]]
    if found["errors"]:
        lines += ["", "Search problems: " + "; ".join(found["errors"])]
    lines += ["", "Nothing was planned, booked or paid. Try other dates, a nearby city, or say where you fly from "
                  "(e.g. 'from LAX')."]
    return "\n".join(lines)


def plan_trip(trip_id: str, raw_text: str, trace=print) -> tuple[str, int]:
    """Run intake + the verification loop. Returns (chat message, version shown)."""
    sig = run_intake(trip_id, raw_text)
    trace(log(sig))
    found = ensure_candidates(trip_id)          # live web search for slots with no options yet
    if found["searched"]:
        trace(f"🔎 web search: {', '.join(found['searched'])}")
    if found["missing"]:
        return no_options_message(trip_id, found), sig.version
    while sig.type not in ("pass", "max_loops"):
        sig = dispatch(sig)
        trace(log(sig))
    from common.db import requests_col
    first = (requests_col().find_one({"trip_id": trip_id}) or {}).get("first_version", 1)
    return gate.build_message(sig, first_version=first)


def approve_and_execute(trip_id: str, version: int, user_text: str, trace=print) -> str:
    sig = gate.approve(trip_id, version, user_text)
    if sig is None:
        return "Not approved. Tell me what to change, or reply 'approve'."
    return execute_approved(sig, trace)


def execute_approved(sig: Signal, trace=print) -> str:
    """From ⚡ pay / ⚡ execute of an approved plan: locker room pays (if new money), vendors execute."""
    trace(log(sig))
    while sig.type != "execute":
        sig = dispatch(sig)
        trace(log(sig))
    return dispatch(sig)
