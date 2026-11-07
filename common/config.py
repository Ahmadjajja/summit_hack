"""Settings from environment (.env). Empty keys mean: use the mock for that part."""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _f(name: str, default: float) -> float:
    v = os.getenv(name, "").split("#")[0].strip()
    return float(v) if v else default


def _s(name: str, default: str = "") -> str:
    return os.getenv(name, default).split("#")[0].strip() or default


MONGODB_URI = _s("MONGODB_URI")
if MONGODB_URI and not MONGODB_URI.startswith(("mongodb://", "mongodb+srv://")):
    print("[config] MONGODB_URI is not a mongodb:// or mongodb+srv:// URI; using in-memory DB instead.")
    MONGODB_URI = ""
MONGODB_DB = _s("MONGODB_DB", "tripcheck")

SUBQ_API_KEY = _s("SUBQ_API_KEY")
SUBQ_BASE_URL = _s("SUBQ_BASE_URL", "https://api.subq.ai/v1")
SUBQ_MODEL = _s("SUBQ_MODEL") or _s("DECISION_MODEL", "subq-snap-1.1")

GEMINI_API_KEY = _s("GEMINI_API_KEY")
GEMINI_MODEL = _s("GEMINI_MODEL") or _s("VERIFIER_MODEL", "gemini-4-argon")
# low | medium | high | off. Higher = more careful but slower (medium ≈ 18s per call vs ≈ 5s off).
VERIFIER_THINKING_LEVEL = _s("VERIFIER_THINKING_LEVEL", "low").lower()
VERIFIER_TIMEOUT_S = _f("VERIFIER_TIMEOUT_S", 60)

AGENT_SEED_PHRASE = _s("AGENT_SEED_PHRASE", "tripcheck-orchestrator-dev-seed")
STRIPE_TEST_KEY = _s("STRIPE_TEST_KEY")
AGENTVERSE_API_KEY = _s("AGENTVERSE_API_KEY")
ASI_ONE_API_KEY = _s("ASI_ONE_API_KEY")
# true = never charge anything real; payments are mock records (Stripe test still allowed if set)
BOOKING_DRY_RUN = _s("BOOKING_DRY_RUN", "true").lower() != "false"
# true = register agents on Agentverse with a mailbox (run on a laptop with internet)
AGENTS_USE_MAILBOX = _s("AGENTS_USE_MAILBOX", "false").lower() == "true"

# Pretend "now" for mid-trip demos, e.g. 2026-11-18T10:00:00 (empty = real time)
DEMO_NOW = _s("DEMO_NOW")

MAX_LOOPS = int(_f("MAX_LOOPS", _f("MAX_REPAIR_ROUNDS", 3)))

SUBQ_PRICE_IN_PER_M = _f("SUBQ_PRICE_IN_PER_M", 0.05)
GEMINI_PRICE_IN_PER_M = _f("GEMINI_PRICE_IN_PER_M", 2.00)
GEMINI_PRICE_OUT_PER_M = _f("GEMINI_PRICE_OUT_PER_M", 10.00)

# MOCK=subq,gemini forces mocks even when keys are set (handy offline or to save credits)
_FORCE_MOCK = {x.strip().lower() for x in _s("MOCK").split(",") if x.strip()}
USE_MOCK_SUBQ = not SUBQ_API_KEY or "subq" in _FORCE_MOCK
USE_MOCK_GEMINI = not GEMINI_API_KEY or "gemini" in _FORCE_MOCK
