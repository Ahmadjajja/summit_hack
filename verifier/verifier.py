"""③ Verifier (owner: P2). On ⚡ check vN: read plans vN + request (incl. raw text)
+ candidates from MongoDB, run Python checks, then a Gemini review. Write
verdicts vN, then:
  PASS                → ⚡ pass   (go straight to the human gate)
  REJECT and N < MAX  → ⚡ replan v(N+1)
  REJECT and N == MAX → ⚡ max_loops (human gate shows the best version)
"""
import json

from common import config
from common.contracts import Issue, PlanPackage, Signal, TripRequest, Verdict
from common.db import clean, get_db, plans_col, requests_col, verdicts_col
from common.metrics import estimate_tokens, track
from verifier.checks import run_all

GEMINI_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {"type": "array", "items": {"type": "object", "properties": {
            "slot": {"type": "string"},
            "why": {"type": "string"},
            "found": {"type": "string"},
            "saves_usd": {"type": "number"},
        }, "required": ["why"]}},
        "summary": {"type": "string"},
    },
    "required": ["issues", "summary"],
}

PROMPT = """You are an independent verifier for a travel plan made by another AI.
You did NOT make this plan. Check it against what the traveller actually wrote.
Deterministic rule results are already computed; do not repeat them.
Look for: anything that contradicts the traveller's raw message, low-confidence
picks that look wrong, a better-value equivalent option the rules missed.
Only report real problems. Use candidate IDs exactly as given.

Traveller's raw message:
{raw}

Plan (version {version}):
{plan}

Rule results:
{rules}
"""


def _gemini_review(req: TripRequest, plan: PlanPackage, checks) -> tuple[list[Issue], int, int]:
    from google import genai  # pip install google-genai
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    prompt = PROMPT.format(raw=req.raw_text, version=plan.version,
                           plan=plan.model_dump_json(indent=1),
                           rules=json.dumps([c.model_dump() for c in checks], indent=1))
    resp = client.models.generate_content(
        model=config.GEMINI_MODEL, contents=prompt,
        config={"response_mime_type": "application/json", "response_schema": GEMINI_SCHEMA},
    )
    data = json.loads(resp.text)
    u = resp.usage_metadata
    issues = [Issue(slot=i.get("slot"), rule="G gemini_review", severity="soft", why=i["why"],
                    found=i.get("found"), saves_usd=i.get("saves_usd") or 0) for i in data.get("issues", [])]
    return issues, (u.prompt_token_count or 0), (u.candidates_token_count or 0)


def run_verifier(signal: Signal) -> Signal:
    trip_id, version = signal.trip_id, signal.version
    plan = PlanPackage(**clean(plans_col().find_one({"trip_id": trip_id, "version": version})))
    req = TripRequest(**clean(requests_col().find_one({"trip_id": trip_id})))
    events = [clean(e) for e in get_db()["events"].find({"id": {"$in": req.events}})]

    model = "mock-gemini" if config.USE_MOCK_GEMINI else config.GEMINI_MODEL
    with track(trip_id, "verifier", model, loop=version, version=version) as m:
        checks, issues = run_all(plan, req, events)
        if config.USE_MOCK_GEMINI:
            m.tokens(estimate_tokens(plan.model_dump_json()), 200)
        else:
            g_issues, tin, tout = _gemini_review(req, plan, checks)
            issues += g_issues
            m.tokens(tin, tout)

    hard = sum(1 for i in issues if i.severity == "hard")
    soft = sum(1 for i in issues if i.severity == "soft")
    verdict = Verdict(
        trip_id=trip_id, version=version, result="PASS" if not issues else "REJECT",
        checks=checks, issues=issues,
        replace={i.slot: i.found for i in issues if i.slot and i.found},
        hard_failures=hard, soft_failures=soft,
    )
    verdicts_col().replace_one({"trip_id": trip_id, "version": version}, verdict.model_dump(), upsert=True)

    if verdict.result == "PASS":
        return Signal(type="pass", trip_id=trip_id, version=version)
    if version >= config.MAX_LOOPS:
        return Signal(type="max_loops", trip_id=trip_id, version=version)
    return Signal(type="replan", trip_id=trip_id, version=version + 1)
