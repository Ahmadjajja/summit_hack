"""Run the whole pipeline locally, no API keys needed (mocks + in-memory Mongo).

  python run_local.py
  python run_local.py "Japan Nov 15-22, Tokyo then Osaka, budget $3,500"

Set keys in .env to switch individual parts to the real APIs.
"""
import sys

from common import config
from orchestrator.pipeline import approve_and_execute, plan_trip
from seed.seed_japan import seed

DEFAULT = "Japan Nov 15-22, Tokyo then Osaka for the dev events, budget $3,500"


def main():
    text = " ".join(sys.argv[1:]) or DEFAULT
    print(f"SubQ: {'MOCK' if config.USE_MOCK_SUBQ else config.SUBQ_MODEL} · "
          f"Gemini: {'MOCK' if config.USE_MOCK_GEMINI else config.GEMINI_MODEL} · "
          f"Mongo: {'in-memory' if not config.MONGODB_URI else 'Atlas'} · max loops {config.MAX_LOOPS}\n")
    seed()
    trip_id = "JP-1115"
    print(f"You: {text}\n")
    msg, version = plan_trip(trip_id, text)
    print("\n--- message to chat ---\n" + msg + "\n")
    print("You: approve\n")
    print(approve_and_execute(trip_id, version, "approve"))


if __name__ == "__main__":
    main()
