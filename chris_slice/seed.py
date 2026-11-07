"""Seed demo TripCheck data so the verifier + gate can run without the planner."""

from __future__ import annotations

from datetime import datetime, timezone

import db

REQUEST_ID = "req_demo_seattle_portland"
PLAN_VERSION = "v1"


def main() -> None:
    now = datetime.now(timezone.utc)

    # Fresh demo slice for this request_id.
    for coll in (db.requests, db.candidates, db.plans, db.verdicts, db.run_log, db.ledger):
        coll.delete_many({"request_id": REQUEST_ID})

    db.requests.insert_one(
        {
            "request_id": REQUEST_ID,
            "origin": "Seattle, WA",
            "destination": "Portland, OR",
            "depart_date": "2026-11-14",
            "return_date": "2026-11-16",
            "party_size": 2,
            "budget_usd": 650,
            "notes": "Weekend trip, prefer Amtrak or nonstop flight, mid-range hotel.",
            "created_at": now,
        }
    )

    candidates = [
        {
            "request_id": REQUEST_ID,
            "option_id": "opt_amtrak_cascades",
            "kind": "transport",
            "label": "Amtrak Cascades round-trip SEA↔PDX",
            "price_usd": 120,
            "origin": "Seattle, WA",
            "destination": "Portland, OR",
            "depart_date": "2026-11-14",
            "return_date": "2026-11-16",
            "party_size": 2,
            "created_at": now,
        },
        {
            "request_id": REQUEST_ID,
            "option_id": "opt_alaska_nonstop",
            "kind": "transport",
            "label": "Alaska Airlines nonstop round-trip SEA↔PDX",
            "price_usd": 210,
            "origin": "Seattle, WA",
            "destination": "Portland, OR",
            "depart_date": "2026-11-14",
            "return_date": "2026-11-16",
            "party_size": 2,
            "created_at": now,
        },
        {
            "request_id": REQUEST_ID,
            "option_id": "opt_hotel_ace",
            "kind": "hotel",
            "label": "Ace Hotel Portland · 2 nights · queen",
            "price_usd": 380,
            "origin": "Portland, OR",
            "destination": "Portland, OR",
            "depart_date": "2026-11-14",
            "return_date": "2026-11-16",
            "party_size": 2,
            "created_at": now,
        },
    ]
    db.candidates.insert_many(candidates)

    plan = {
        "request_id": REQUEST_ID,
        "version": PLAN_VERSION,
        "status": "planned",
        "loop": 0,
        "summary": "Amtrak Cascades + Ace Hotel Portland for a 2-night weekend.",
        "selected_option_ids": ["opt_amtrak_cascades", "opt_hotel_ace"],
        "total_usd": 500,
        "new_money_usd": 500,
        "origin": "Seattle, WA",
        "destination": "Portland, OR",
        "depart_date": "2026-11-14",
        "return_date": "2026-11-16",
        "party_size": 2,
        "created_at": now,
    }
    result = db.plans.insert_one(plan)

    print(f"Seeded request {REQUEST_ID}")
    print(f"Seeded {len(candidates)} candidates")
    print(f"Seeded plan {result.inserted_id} version={PLAN_VERSION} status=planned total_usd=500")
    print("Next: python verifier.py")


if __name__ == "__main__":
    main()
