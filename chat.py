"""Chat with TripCheck in the terminal, using the real APIs from .env.

    python chat.py                       # new trip
    DEMO_NOW=2026-11-18T10:00:00 python chat.py   # pretend you're mid-trip for changes

Type a request, then "approve" to execute it. Follow-ups go to the same trip:
  Japan Nov 15-22, Tokyo then Osaka for the dev events, budget $3,500
  approve
  Stay 2 more nights in Osaka
  approve
  Cancel my Osaka hotel
  approve
Commands: /trip (show bookings + payments), /quit
"""
import sys

from common import config
from common.db import ledger_col
from orchestrator.pipeline import approve_and_execute, plan_trip
from seed.seed_japan import seed

TRIP = "TRIP-1"


def show_trip():
    rows = list(ledger_col().find({"trip_id": TRIP}))
    for b in [r for r in rows if r["type"] == "booking"]:
        when = f"{b.get('check_in')} → {b.get('check_out')}" if b["kind"] == "hotel" else f"{b.get('date')} {b.get('dep', '')}"
        print(f"  {b['status']:9s} {b['id']:9s} {b['kind']:7s} {when:24s} ${b['cost_usd']:,.0f}")
    paid = sum(r["amount_usd"] for r in rows if r["type"] == "payment" and r["status"] == "completed")
    back = sum(r["amount_usd"] for r in rows if r["type"] == "refund")
    print(f"  paid ${paid:,.0f} · refunded ${back:,.0f}")


def main():
    print(f"TripCheck · SubQ: {'MOCK' if config.USE_MOCK_SUBQ else config.SUBQ_MODEL} · "
          f"Gemini: {'MOCK' if config.USE_MOCK_GEMINI else config.GEMINI_MODEL} · "
          f"Mongo: {'Atlas' if config.MONGODB_URI else 'in-memory'}"
          + (f" · now = {config.DEMO_NOW}" if config.DEMO_NOW else ""))
    seed(reset=not config.MONGODB_URI)
    print("Type a trip request. 'approve' to execute. /trip to see bookings. /quit to exit.\n")
    waiting = None
    interactive = sys.stdin.isatty()
    while True:
        try:
            text = input("You: ").strip() if interactive else sys.stdin.readline()
        except (EOFError, KeyboardInterrupt):
            break
        if not interactive:
            if not text:
                break
            text = text.strip()
            print(f"You: {text}")
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        if text == "/trip":
            show_trip()
            continue
        if waiting and "approve" in text.lower():
            print("\n" + approve_and_execute(TRIP, waiting, text, trace=lambda *_: None) + "\n")
            waiting = None
            continue
        msg, waiting = plan_trip(TRIP, text, trace=lambda s: print("  " + s))
        print("\n" + msg + "\n")


if __name__ == "__main__":
    main()
