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
ASI_ONE_BASE_URL = _s("ASI_ONE_BASE_URL", "https://api.asi1.ai/v1")
ASI_ONE_MODEL = _s("ASI_ONE_MODEL", "asi1")
# Live web search (Fetch.ai ASI:One) for trips the seed data doesn't cover. false = never search.
LIVE_SEARCH = _s("LIVE_SEARCH", "true").lower() != "false"
SEARCH_TIMEOUT_S = _f("SEARCH_TIMEOUT_S", 150)
DEFAULT_ORIGIN = _s("DEFAULT_ORIGIN", "SFO")   # where flights leave from when the traveller doesn't say
# Stripe Checkout (test mode) in the web UI: the traveller pays on Stripe's page with a test card.
STRIPE_CHECKOUT = _s("STRIPE_CHECKOUT", "true").lower() != "false"
# ASI:One agent: Fetch.ai-style embedded Checkout (RequestPayment metadata["stripe"]) needs the
# publishable key too; without it the agent sends a hosted Checkout link instead.
STRIPE_PUBLISHABLE_KEY = _s("STRIPE_PUBLISHABLE_KEY")
if STRIPE_PUBLISHABLE_KEY and not STRIPE_PUBLISHABLE_KEY.startswith("pk_test_"):
    print("[config] STRIPE_PUBLISHABLE_KEY is not a pk_test_ key; ignoring it (test mode only).")
    STRIPE_PUBLISHABLE_KEY = ""
STRIPE_SUCCESS_URL = _s("STRIPE_SUCCESS_URL", "https://agentverse.ai/payment-success")
PUBLIC_URL = _s("PUBLIC_URL", "http://localhost:8080").rstrip("/")   # where Stripe sends the traveller back
# true = never charge anything real; payments are mock records (Stripe test still allowed if set)
BOOKING_DRY_RUN = _s("BOOKING_DRY_RUN", "true").lower() != "false"
# true = register the chat agent on Agentverse with a mailbox (needs internet)
AGENTS_USE_MAILBOX = _s("AGENTS_USE_MAILBOX", "false").lower() == "true"
# true = also register the 5 booking agents on Agentverse (they work locally without it)
BOOKING_AGENTS_MAILBOX = _s("BOOKING_AGENTS_MAILBOX", "false").lower() == "true"

# true = make uagents' HTTP client use HTTPS_PROXY / system proxy settings (corporate or sandboxed networks)
if _s("AIOHTTP_TRUST_ENV", "false").lower() == "true":
    import aiohttp
    _orig_init = aiohttp.ClientSession.__init__

    def _init(self, *a, **kw):
        kw.setdefault("trust_env", True)
        _orig_init(self, *a, **kw)
    aiohttp.ClientSession.__init__ = _init

# Pretend "now" for mid-trip demos, e.g. 2026-11-18T10:00:00 (empty = real time)
DEMO_NOW = _s("DEMO_NOW")

MAX_LOOPS = int(_f("MAX_LOOPS", _f("MAX_REPAIR_ROUNDS", 3)))

SUBQ_PRICE_IN_PER_M = _f("SUBQ_PRICE_IN_PER_M", 0.05)
GEMINI_PRICE_IN_PER_M = _f("GEMINI_PRICE_IN_PER_M", 2.00)
GEMINI_PRICE_OUT_PER_M = _f("GEMINI_PRICE_OUT_PER_M", 10.00)
ASI_PRICE_IN_PER_M = _f("ASI_PRICE_IN_PER_M", 1.00)    # confirm on the ASI:One console
ASI_PRICE_OUT_PER_M = _f("ASI_PRICE_OUT_PER_M", 5.00)

# MOCK=subq,gemini forces mocks even when keys are set (handy offline or to save credits)
_FORCE_MOCK = {x.strip().lower() for x in _s("MOCK").split(",") if x.strip()}
USE_MOCK_SUBQ = not SUBQ_API_KEY or "subq" in _FORCE_MOCK
USE_MOCK_GEMINI = not GEMINI_API_KEY or "gemini" in _FORCE_MOCK
USE_MOCK_ASI = not ASI_ONE_API_KEY or "asi" in _FORCE_MOCK
