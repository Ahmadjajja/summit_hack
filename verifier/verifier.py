"""③ Verifier (owner: P2). On ⚡ check vN: read plans vN + request (incl. the
traveller's raw text) + candidates/ledger from MongoDB, run the Python rules,
then an independent Gemini review. Write verdicts vN, then:

  PASS                → ⚡ pass       (go straight to the human gate)
  REJECT and N < MAX  → ⚡ replan v(N+1)
  REJECT and N == MAX → ⚡ max_loops  (human gate shows the best version)

Decision rule (so loops are never wasted):
  REJECT  if any HARD issue, or any SOFT issue that comes with a concrete fix
          (a replacement candidate the planner can switch to)
  PASS    otherwise; remaining soft issues are shown to the traveller as warnings

Gemini is a reviewer, not the judge of numbers: it can add soft issues and
suggest replacements, but every replacement must be a real candidate in the same
slot or it is dropped. If Gemini is unavailable the verdict is rules-only.
"""
import json

from common import config
from common.contracts import Issue, PlanPackage, Signal, TripRequest, Verdict
from common.db import clean, get_db, plans_col, requests_col, verdicts_col
from common.metrics import estimate_tokens, track
from verifier.checks import is_change, itinerary, run_all

GEMINI_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {"type": "array", "items": {"type": "object", "properties": {
            "slot": {"type": "string", "description": "slot name from the plan, or empty"},
            "why": {"type": "string"},
            "found": {"type": "string", "description": "replacement candidate id from that slot, or empty"},
            "saves_usd": {"type": "number"},
        }, "required": ["why"]}},
        "summary": {"type": "string", "description": "2-3 sentences for the traveller"},
    },
    "required": ["issues", "summary"],
}

PROMPT = """You are an independent verifier. Another AI made this travel plan; you did NOT.
Check it against what the traveller actually wrote. Deterministic rule results are
already computed below; do not repeat them or redo their arithmetic.

Report only real problems the rules can't see, for example:
- the plan contradicts something in the traveller's message (preferences, constraints, intent)
- a pick with low planner confidence that looks wrong for this traveller
- a better-value equivalent option in the SAME slot that the rules missed
If you suggest a replacement, `found` must be a candidate id listed in that slot.
If nothing is wrong, return an empty issues list.

Traveller's message:
{raw}

Plan v{version} ({kind}):
{plan}

Trip after this plan is executed:
{trip}

Rule results:
{rules}
"""


def _compact_plan(plan: PlanPackage) -> str:
    if is_change(plan):
        return json.dumps([a.model_dump() for a in plan.actions], indent=1)
    return json.dumps([{"slot": s.slot, "pick": s.pick, "cost_usd": s.cost_usd, "confidence": s.confidence,
                        "pick_detail": s.pick_detail,
                        "candidates": [{k: c.get(k) for k in ("id", "price_usd", "nightly_usd", "dep", "date",
                                                              "area", "rating", "name", "airline", "distance_mi")
                                        if c.get(k) is not None} for c in s.candidates]}
                       for s in plan.slots], indent=1)


def _gemini_review(req: TripRequest, plan: PlanPackage, checks, trip) -> tuple[list[Issue], str, int, int]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.GEMINI_API_KEY,
                          http_options=types.HttpOptions(timeout=int(config.VERIFIER_TIMEOUT_S * 1000)))
    prompt = PROMPT.format(raw=req.raw_text, version=plan.version, kind=plan.intent,
                           plan=_compact_plan(plan),
                           trip=json.dumps([{k: t.get(k) for k in ("id", "kind", "city", "date", "dep", "check_in",
                                                                   "check_out", "cost_usd") if t.get(k) is not None}
                                            for t in trip], indent=1),
                           rules=json.dumps([c.model_dump() for c in checks], indent=1))
    extra = {}
    if config.VERIFIER_THINKING_LEVEL in ("low", "medium", "high"):
        extra["thinking_config"] = types.ThinkingConfig(thinking_level=config.VERIFIER_THINKING_LEVEL)
    resp = client.models.generate_content(
        model=config.GEMINI_MODEL, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json",
                                           response_schema=GEMINI_SCHEMA, **extra),
    )
    data = json.loads(resp.text)
    u = resp.usage_metadata
    out_tokens = (u.candidates_token_count or 0) + (getattr(u, "thoughts_token_count", None) or 0)
    return validate_gemini_issues(plan, data.get("issues", [])), data.get("summary", ""), \
        (u.prompt_token_count or 0), out_tokens


def validate_gemini_issues(plan: PlanPackage, raw: list[dict]) -> list[Issue]:
    """Gemini's issues become soft issues. A suggested replacement survives only if it
    is a real, not-yet-rejected candidate in the same slot; otherwise the note stays
    but the fix is dropped (so a hallucinated ID can't send the planner anywhere)."""
    slots = {s.slot: s for s in plan.slots}
    issues = []
    for i in raw:
        slot, found = (i.get("slot") or None), (i.get("found") or None)
        s = slots.get(slot)
        if found and (s is None or found == s.pick or found not in {c["id"] for c in s.candidates}
                      or found in plan.excluded_ids):
            found = None  # not a real alternative in that slot: keep the note, drop the fix
        issues.append(Issue(slot=slot if s else None, rule="G gemini_review", severity="soft",
                            why=i.get("why", ""), found=found,
                            saves_usd=float(i.get("saves_usd") or 0) if found else 0))
    return issues


def decide(issues: list[Issue]) -> str:
    fixable = any(i.severity == "soft" and i.found for i in issues)
    return "REJECT" if any(i.severity == "hard" for i in issues) or fixable else "PASS"


def run_verifier(signal: Signal) -> Signal:
    trip_id, version = signal.trip_id, signal.version
    plan = PlanPackage(**clean(plans_col().find_one({"trip_id": trip_id, "version": version})))
    req = TripRequest(**clean(requests_col().find_one({"trip_id": trip_id})))
    events = [clean(e) for e in get_db()["events"].find({"id": {"$in": req.events}})]

    summary, reviewer = "", "rules-only (mock)"
    model = "mock-gemini" if config.USE_MOCK_GEMINI else config.GEMINI_MODEL
    with track(trip_id, "verifier", model, loop=version, version=version) as m:
        checks, issues = run_all(plan, req, events)
        if config.USE_MOCK_GEMINI:
            m.tokens(estimate_tokens(plan.model_dump_json()), 200)
        else:
            try:
                g_issues, summary, tin, tout = _gemini_review(req, plan, checks, itinerary(plan))
                issues += g_issues
                m.tokens(tin, tout)
                reviewer = f"{config.GEMINI_MODEL} (thinking {config.VERIFIER_THINKING_LEVEL})"
            except Exception as e:  # never let the reviewer's outage block the deterministic verdict
                reviewer = f"rules-only (Gemini unavailable: {type(e).__name__})"
                m.note = str(e)[:300]

    replace = {}
    for i in issues:
        if i.slot and i.found and i.slot not in replace:
            replace[i.slot] = i.found
    verdict = Verdict(
        trip_id=trip_id, version=version, result=decide(issues), checks=checks, issues=issues,
        replace=replace,
        hard_failures=sum(1 for i in issues if i.severity == "hard"),
        soft_failures=sum(1 for i in issues if i.severity == "soft"),
        summary=summary, reviewer=reviewer,
    )
    verdicts_col().replace_one({"trip_id": trip_id, "version": version}, verdict.model_dump(), upsert=True)

    if verdict.result == "PASS":
        return Signal(type="pass", trip_id=trip_id, version=version)
    loop = version - req.first_version + 1
    if loop >= config.MAX_LOOPS:
        return Signal(type="max_loops", trip_id=trip_id, version=version)
    return Signal(type="replan", trip_id=trip_id, version=version + 1)
