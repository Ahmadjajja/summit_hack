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
MONGODB_DB = _s("MONGODB_DB", "tripcheck")

SUBQ_API_KEY = _s("SUBQ_API_KEY")
SUBQ_BASE_URL = _s("SUBQ_BASE_URL", "https://api.subq.ai/v1")
SUBQ_MODEL = _s("SUBQ_MODEL", "subq-snap-1.1")

GEMINI_API_KEY = _s("GEMINI_API_KEY")
GEMINI_MODEL = _s("GEMINI_MODEL", "gemini-4-argon")

AGENT_SEED_PHRASE = _s("AGENT_SEED_PHRASE", "tripcheck-orchestrator-dev-seed")
STRIPE_TEST_KEY = _s("STRIPE_TEST_KEY")

MAX_LOOPS = int(_f("MAX_LOOPS", 3))

SUBQ_PRICE_IN_PER_M = _f("SUBQ_PRICE_IN_PER_M", 0.05)
GEMINI_PRICE_IN_PER_M = _f("GEMINI_PRICE_IN_PER_M", 2.00)
GEMINI_PRICE_OUT_PER_M = _f("GEMINI_PRICE_OUT_PER_M", 10.00)

USE_MOCK_SUBQ = not SUBQ_API_KEY
USE_MOCK_GEMINI = not GEMINI_API_KEY
