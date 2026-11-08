"""② Planner (owner: P1). On ⚡ ready / ⚡ replan: read everything from MongoDB,
pick one candidate per slot, write plans vN, then ⚡ check vN.

SubQ Snap does the picking: one *choice* question per slot, with every candidate
as an option (labels = candidate IDs). Our code assembles the PlanPackage JSON.
Candidates rejected in earlier loops are excluded. The verifier's suggested
replacements are offered as context.
"""
import json

from common import config
from common.contracts import Action, PlanPackage, Signal, SlotPick, TripRequest
from common.db import candidates_col, clean, plans_col, requests_col, verdicts_col
from common.metrics import estimate_tokens, track
from intake import subq_client


def candidate_cost(c: dict) -> float:
    if c["kind"] == "hotel":
        from datetime import date
        nights = (date.fromisoformat(c["check_out"]) - date.fromisoformat(c["check_in"])).days
        return float(c["nightly_usd"] * nights)
    return float(c["price_usd"])


def _load(trip_id: str, version: int):
    req = TripRequest(**clean(requests_col().find_one({"trip_id": trip_id})))
    cands = [clean(c) for c in candidates_col().find({
        "$or": [
            # flights of THIS trip only: out to one of its cities on day 1, back from one on the last day
            # (before: every flight in the date range, so another trip's flights could be picked)
            {"kind": "flight", "slot": "flight_out", "date": req.start, "to": {"$in": req.cities}},
            {"kind": "flight", "slot": "flight_ret", "date": req.end, "from": {"$in": req.cities}},
            {"kind": "hotel", "city": {"$in": req.cities},
             "check_in": {"$gte": req.start}, "check_out": {"$lte": req.end}},
            {"kind": "dining", "city": {"$in": req.cities}, "date": {"$gte": req.start, "$lte": req.end}},
        ],
    })]
    # If a city has hotels for exactly its planned stay, don't mix in other date spans in that slot
    from scout.scout import split_nights
    stays = {c: (ci, co) for c, ci, co in split_nights(req.cities, req.start, req.end)}
    exact = {c["city"] for c in cands if c["kind"] == "hotel" and stays.get(c["city"]) == (c["check_in"], c["check_out"])}
    cands = [c for c in cands if c["kind"] != "hotel" or c["city"] not in exact
             or stays[c["city"]] == (c["check_in"], c["check_out"])]
    excluded, hints = [], {}
    for v in verdicts_col().find({"trip_id": trip_id,
                                  "version": {"$gte": req.first_version, "$lt": version}}):
        if any(i["rule"].startswith("R-01") for i in v.get("issues", [])):
            hints["_over_budget"] = True
        prev = plans_col().find_one({"trip_id": trip_id, "version": v["version"]})
        for issue in v.get("issues", []):
            # drop the old pick only when there's a concrete fix or it broke a hard rule;
            # a note without a replacement shouldn't shrink the options
            if issue.get("slot") and prev and (issue.get("found") or issue.get("severity") == "hard"):
                picked = next((s["pick"] for s in prev["slots"] if s["slot"] == issue["slot"]), None)
                if picked:
                    excluded.append(picked)
        hints.update(v.get("replace", {}))
    return req, cands, sorted(set(excluded)), hints


def _mock_pick(slot: str, options: list[dict], over_budget: bool = False) -> dict:
    """Stand-in for SubQ: favour convenience (closeness to events); after a
    budget rejection, favour the cheapest option instead."""
    if over_budget:
        best = min(options, key=candidate_cost)
    else:
        best = max(options, key=lambda c: c.get("convenience", 0))
    total = sum(c.get("convenience", 0) for c in options) or 1
    return {"choice": best["id"], "confidence": round(best.get("convenience", 0) / total, 2),
            "probs": {c["id"]: round(c.get("convenience", 0) / total, 3) for c in options}}


def _subq_pick(req: TripRequest, by_slot: dict[str, list[dict]], hints: dict) -> tuple[dict, int]:
    state = (
        f"Trip request:\n{req.model_dump_json(indent=1)}\n\n"
        f"Verifier notes from the previous loop (suggested replacements; _over_budget means cut cost): {json.dumps(hints)}\n\n"
        "Candidates by slot:\n" + json.dumps(by_slot, indent=1)
    )
    # A slot with a single option has nothing to decide (SubQ needs 2+ options): take it.
    answers = {s: {"choice": opts[0]["id"], "confidence": 1.0, "probs": {opts[0]["id"]: 1.0}}
               for s, opts in by_slot.items() if len(opts) == 1}
    tokens = 0
    slots = [s for s in by_slot if len(by_slot[s]) >= 2]
    for i in range(0, len(slots), 16):  # Snap takes at most 16 questions per call
        qs = {
            s: subq_client.choice_q(
                f"Pick the best option for slot '{s}' for this traveller: fit the dates, "
                f"stay near their events, respect the budget, prefer good value.",
                {c["id"]: f"{c.get('name') or c.get('airline') or c.get('area')} · ${candidate_cost(c):.0f}"
                 for c in by_slot[s]})
            for s in slots[i:i + 16]
        }
        res = subq_client.decide(state, qs)
        answers.update(res["answers"])
        tokens += subq_client.input_tokens(res["usage"], state)
    return answers, tokens


def run_planner(signal: Signal) -> Signal:
    trip_id, version = signal.trip_id, signal.version or 1
    req = TripRequest(**clean(requests_col().find_one({"trip_id": trip_id})))
    if req.intent != "book":                       # cancel / reschedule / extend
        from planner.changes import run_change_planner
        return run_change_planner(Signal(type="replan", trip_id=trip_id, version=version), req)
    req, cands, excluded, hints = _load(trip_id, version)

    by_slot: dict[str, list[dict]] = {}
    for c in cands:
        if c["id"] not in excluded:
            by_slot.setdefault(c["slot"], []).append(c)

    model = "mock-subq" if config.USE_MOCK_SUBQ else config.SUBQ_MODEL
    with track(trip_id, "planner", model, loop=version, version=version) as m:
        if config.USE_MOCK_SUBQ:
            answers = {s: _mock_pick(s, opts, hints.get("_over_budget", False)) for s, opts in by_slot.items()}
            m.tokens(estimate_tokens(json.dumps(by_slot)))
        else:
            answers, toks = _subq_pick(req, by_slot, hints)
            m.tokens(toks)

    slots = []
    for s, opts in sorted(by_slot.items()):
        a = answers[s]
        pick = next(c for c in opts if c["id"] == a["choice"])
        slots.append(SlotPick(slot=s, pick=pick["id"], pick_detail=pick,
                              confidence=float(a.get("confidence") or 0), probs=a.get("probs") or {},
                              candidates=opts, cost_usd=candidate_cost(pick)))
    total = round(sum(s.cost_usd for s in slots), 2)
    plan = PlanPackage(
        trip_id=trip_id, version=version, intent=req.intent, slots=slots,
        actions=[Action(op="book", id=s.pick, cost_usd=s.cost_usd) for s in slots],
        total_usd=total, money_delta_usd=total, excluded_ids=excluded,
    )
    plans_col().replace_one({"trip_id": trip_id, "version": version}, plan.model_dump(), upsert=True)
    return Signal(type="check", trip_id=trip_id, version=version)
