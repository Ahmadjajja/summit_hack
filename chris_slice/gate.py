"""Human approval gate: chat-style CLI for verified TripCheck plans."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import db


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def locker_room_payment(plan: dict[str, Any]) -> dict[str, Any]:
    """Stub payment branch. A real integration would charge the card / wallet here."""
    amount = float(plan.get("new_money_usd") or 0)
    entry = {
        "type": "payment",
        "plan_id": plan["_id"],
        "request_id": plan.get("request_id"),
        "amount": amount,
        "status": "pending",
        "created_at": _utc_now(),
    }
    result = db.ledger.insert_one(entry)
    entry["_id"] = result.inserted_id
    print(
        f"[locker_room] would charge ${amount:.2f} for plan {plan['_id']} "
        f"(ledger entry {result.inserted_id}, status=pending)"
    )
    return entry


def scoreboard_for_request(request_id: str) -> dict[str, Any]:
    verify_logs = list(
        db.run_log.find({"request_id": request_id, "stage": "verify"}).sort("created_at", 1)
    )
    verdicts = list(db.verdicts.find({"request_id": request_id}).sort("created_at", 1))
    history = [v.get("verdict") for v in verdicts]
    total_ms = sum(int(row.get("latency_ms") or 0) for row in verify_logs)
    total_cost = sum(float(row.get("cost_usd") or 0) for row in verify_logs)
    plan_loops = max((int(p.get("loop") or 0) for p in db.plans.find({"request_id": request_id})), default=0)
    return {
        "plan_loops": plan_loops,
        "history": history,
        "total_verifier_ms": total_ms,
        "total_verifier_usd": total_cost,
        "verify_runs": len(verify_logs),
    }


def print_plan_and_scoreboard(plan: dict[str, Any], board: dict[str, Any]) -> None:
    print("\n" + "=" * 60)
    print("TRIPCHECK · needs your approval")
    print("=" * 60)
    print(f"plan_id:     {plan.get('_id')}")
    print(f"request_id:  {plan.get('request_id')}")
    print(f"version:     {plan.get('version')}")
    print(f"summary:     {plan.get('summary')}")
    print(f"route:       {plan.get('origin')} → {plan.get('destination')}")
    print(f"dates:       {plan.get('depart_date')} → {plan.get('return_date')}")
    print(f"party_size:  {plan.get('party_size')}")
    print(f"total_usd:   {plan.get('total_usd')}")
    print(f"new_money:   ${float(plan.get('new_money_usd') or 0):.2f}")
    print(f"options:     {plan.get('selected_option_ids')}")
    print("-" * 60)
    print("SCOREBOARD")
    print(f"  plan loops:        {board['plan_loops']}")
    print(f"  PASS/REJECT hist:  {board['history']}")
    print(f"  verifier runs:     {board['verify_runs']}")
    print(f"  total verifier ms: {board['total_verifier_ms']}")
    print(f"  total verifier $:  {board['total_verifier_usd']:.6f}")
    print("=" * 60)


def handle_decision(plan: dict[str, Any], decision: str) -> None:
    plan_id = plan["_id"]
    created_at = _utc_now()
    decision = decision.strip().lower()

    if decision in ("approve", "a", "yes", "y"):
        new_money = float(plan.get("new_money_usd") or 0)
        if new_money > 0:
            locker_room_payment(plan)
        db.plans.update_one({"_id": plan_id}, {"$set": {"status": "executed"}})
        outcome = "executed"
        print(f"[approve] plan {plan_id} → executed")
    elif decision in ("reject", "r", "no", "n"):
        db.plans.update_one({"_id": plan_id}, {"$set": {"status": "cancelled"}})
        outcome = "cancelled"
        print(f"[reject] plan {plan_id} → cancelled")
    else:
        print("Please type approve or reject.")
        return

    db.run_log.insert_one(
        {
            "stage": "approve",
            "plan_id": plan_id,
            "request_id": plan.get("request_id"),
            "version": plan.get("version"),
            "decision": decision,
            "outcome": outcome,
            "created_at": created_at,
        }
    )


def main() -> None:
    print("TripCheck human approval gate")
    print("Type approve / reject when a verified plan appears. Ctrl+C to quit.\n")

    while True:
        plan = db.plans.find_one({"status": "verified"}, sort=[("created_at", 1)])
        if plan is None:
            print("… no verified plans; waiting 3s")
            time.sleep(3)
            continue

        board = scoreboard_for_request(plan["request_id"])
        print_plan_and_scoreboard(plan, board)
        decision = input("approve / reject > ").strip()
        handle_decision(plan, decision)


if __name__ == "__main__":
    main()
