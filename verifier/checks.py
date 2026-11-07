"""Deterministic verifier rules (owner: P2). Pure Python: same input, same result.

Hard rule failing → plan cannot be approved. Soft rule → can be approved anyway.
See SPEC.md section 4.
"""
from datetime import date, timedelta

from common.contracts import Check, Issue, PlanPackage, TripRequest


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def r01_budget(plan: PlanPackage, req: TripRequest):
    ok = plan.total_usd <= req.budget_usd
    detail = f"total ${plan.total_usd:,.0f} vs budget ${req.budget_usd:,.0f}"
    issues = [] if ok else [Issue(rule="R-01 budget", severity="hard",
                                  why=f"over budget by ${plan.total_usd - req.budget_usd:,.0f} ({detail})")]
    return Check(rule="R-01 budget", ok=ok, severity="hard", detail=detail), issues


def _hotels(plan):
    return [s for s in plan.slots if s.pick_detail.get("kind") == "hotel"]


def r05_no_overlap(plan: PlanPackage, req: TripRequest):
    nights = {}
    for s in _hotels(plan):
        d, end = _d(s.pick_detail["check_in"]), _d(s.pick_detail["check_out"])
        while d < end:
            nights.setdefault(d, []).append(s.pick)
            d += timedelta(days=1)
    problems, d = [], _d(req.start)
    while d < _d(req.end):
        n = len(nights.get(d, []))
        if n == 0:
            problems.append(f"{d} has no hotel")
        elif n > 1:
            problems.append(f"{d} has {n} hotels")
        d += timedelta(days=1)
    issues = [Issue(rule="R-05 no_overlap", severity="hard", why=p) for p in problems]
    return Check(rule="R-05 no_overlap", ok=not problems, severity="hard",
                 detail="; ".join(problems) or "every night covered once"), issues


def r09_flights(plan: PlanPackage, req: TripRequest):
    out = next((s for s in plan.slots if s.slot == "flight_out"), None)
    ret = next((s for s in plan.slots if s.slot == "flight_ret"), None)
    problems = []
    if not out or out.pick_detail["date"] != req.start:
        problems.append(f"outbound flight not on {req.start}")
    if not ret or ret.pick_detail["date"] != req.end:
        problems.append(f"return flight not on {req.end}")
    issues = [Issue(rule="R-09 flight_matches_dates", severity="hard", why=p) for p in problems]
    return Check(rule="R-09 flight_matches_dates", ok=not problems, severity="hard",
                 detail="; ".join(problems) or "flights match trip dates"), issues


def r04_events(plan: PlanPackage, req: TripRequest, events: list[dict]):
    problems = []
    for e in events:
        ed = _d(e["date"])
        here = any(s.pick_detail.get("city") == e["city"]
                   and _d(s.pick_detail["check_in"]) <= ed < _d(s.pick_detail["check_out"])
                   for s in _hotels(plan))
        if not here:
            problems.append(f"not in {e['city']} on {e['date']} for {e['name']}")
    issues = [Issue(rule="R-04 events_covered", severity="hard", why=p) for p in problems]
    return Check(rule="R-04 events_covered", ok=not problems, severity="hard",
                 detail="; ".join(problems) or "all events covered"), issues


def r20_cheaper_equivalent(plan: PlanPackage, req: TripRequest):
    issues = []
    for s in plan.slots:
        p = s.pick_detail
        best = None
        for c in s.candidates:
            if c["id"] == s.pick or c["id"] in plan.excluded_ids:
                continue
            from planner.planner import candidate_cost
            if candidate_cost(c) > 0.9 * s.cost_usd:
                continue
            if p["kind"] == "flight":
                same = c["date"] == p["date"] and abs(_minutes(c["dep"]) - _minutes(p["dep"])) <= 90
            else:
                same = c.get("area") == p.get("area") and c.get("rating", 0) >= p.get("rating", 0) - 0.2
            if same and (best is None or candidate_cost(c) < candidate_cost(best)):
                best = c
        if best:
            from planner.planner import candidate_cost
            save = s.cost_usd - candidate_cost(best)
            what = (f"${candidate_cost(best):,.0f} vs ${s.cost_usd:,.0f}, same day, departs {best['dep']} vs {p['dep']}"
                    if p["kind"] == "flight"
                    else f"${candidate_cost(best):,.0f} vs ${s.cost_usd:,.0f}, same area, rating {best.get('rating')}")
            issues.append(Issue(slot=s.slot, rule="R-20 cheaper_equivalent", severity="soft",
                                why=what, found=best["id"], saves_usd=round(save, 2)))
    return Check(rule="R-20 cheaper_equivalent", ok=not issues, severity="soft",
                 detail=f"{len(issues)} cheaper equivalent(s) found"), issues


def run_all(plan: PlanPackage, req: TripRequest, events: list[dict]):
    checks, issues = [], []
    for fn in (r01_budget, r05_no_overlap, r09_flights):
        c, i = fn(plan, req)
        checks.append(c)
        issues += i
    c, i = r04_events(plan, req, events)
    checks.append(c)
    issues += i
    c, i = r20_cheaper_equivalent(plan, req)
    checks.append(c)
    issues += i
    # TODO(P2): R-12 refund_matches_policy for cancel plans (policy in hotel candidate).
    return checks, issues
