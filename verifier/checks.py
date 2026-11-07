"""Deterministic verifier rules (owner: P2). Pure Python: same input, same result.

Works for both kinds of plan:
  - book plans: the itinerary is the planner's picks (plan.slots)
  - change plans (cancel / reschedule / extend): the itinerary is the traveller's
    ACTIVE bookings in the ledger with the plan's actions applied, so we check the
    trip as it will look after the change, not just the change itself.

Hard rule failing → plan can't be approved. Soft → shown as a warning.
For a cancel the traveller asked for, gaps in the trip are expected, so the
coverage rules (R-04, R-05, R-09) become soft warnings.

  R-01 budget                  total trip cost ≤ budget (soft for changes)
  R-04 events_covered          in the event's city on the event's date
  R-05 no_overlap              every night between flights has exactly one hotel
  R-09 flights_match_hotels    outbound = first check-in, return = last check-out
                               (book plans: also = requested trip dates)
  R-12 refund_matches_policy   cancel refund = vendor policy refund
  R-13 change_price_matches    modify price = vendor policy price
  R-14 targets_exist           changed/cancelled bookings are active on this trip
  R-20 cheaper_equivalent      (soft) same-slot option ≥10% cheaper and equivalent
"""
from datetime import date, timedelta

from common.contracts import Action, Check, Issue, PlanPackage, TripRequest
from common.db import clean, ledger_col
from vendors import policy


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_change(plan: PlanPackage) -> bool:
    return plan.intent != "book" or any(a.op != "book" for a in plan.actions)


# ------------------------------------------------------------------ itinerary
def active_bookings(trip_id: str) -> dict[str, dict]:
    return {b["id"]: clean(b) for b in ledger_col().find({"type": "booking", "trip_id": trip_id, "status": "active"})}


def itinerary(plan: PlanPackage) -> list[dict]:
    """The trip as it will be if this plan is executed."""
    if not is_change(plan):
        return [{**s.pick_detail, "cost_usd": s.cost_usd} for s in plan.slots]
    items = active_bookings(plan.trip_id)
    for a in plan.actions:
        if a.op == "cancel":
            items.pop(a.id, None)
        elif a.op == "book":
            try:
                c = policy.candidate(a.id)
            except KeyError:
                continue                           # R-14 reports it
            items[a.id] = {**c, "cost_usd": a.cost_usd}
        elif a.op == "modify" and a.id in items:
            b = dict(items[a.id])
            if b["kind"] == "hotel" and "new_check_out" in a.params:
                b["check_in"] = a.params.get("new_check_in", b["check_in"])
                b["check_out"] = a.params["new_check_out"]
                b["cost_usd"] = policy._nights(b["check_in"], b["check_out"]) * b["nightly_usd"]
            elif b["kind"] == "flight" and "new_flight" in a.params:
                try:
                    new = policy.candidate(a.params["new_flight"])
                    b.update(date=new["date"], dep=new["dep"])
                except KeyError:
                    pass
                b["cost_usd"] = b["cost_usd"] + a.cost_usd
            items[a.id] = b
    return list(items.values())


def _flights(items):
    fl = sorted((i for i in items if i["kind"] == "flight"), key=lambda i: (i["date"], i.get("dep", "")))
    return (fl[0], fl[-1]) if len(fl) >= 2 else (fl[0] if fl else None, None)


def _hotels(items):
    return [i for i in items if i["kind"] == "hotel"]


def _span(items, req: TripRequest, change: bool) -> tuple[str, str] | None:
    if not change:
        return req.start, req.end
    out, ret = _flights(items)
    hotels = _hotels(items)
    start = out["date"] if out else (min(h["check_in"] for h in hotels) if hotels else None)
    end = ret["date"] if ret else (max(h["check_out"] for h in hotels) if hotels else None)
    return (start, end) if start and end else None


# ------------------------------------------------------------------ rules
def r01_budget(plan, req, items, change):
    total = round(sum(i.get("cost_usd", 0) for i in items), 2) if change else plan.total_usd
    ok = total <= req.budget_usd
    sev = "soft" if change else "hard"
    detail = f"trip total ${total:,.0f} vs budget ${req.budget_usd:,.0f}"
    issues = [] if ok else [Issue(rule="R-01 budget", severity=sev,
                                  why=f"over budget by ${total - req.budget_usd:,.0f} ({detail})")]
    return Check(rule="R-01 budget", ok=ok, severity=sev, detail=detail), issues


def r05_no_overlap(plan, req, items, span, sev):
    if not span:
        return Check(rule="R-05 no_overlap", ok=True, severity=sev, detail="no stay to check"), []
    nights: dict[date, list[str]] = {}
    for h in _hotels(items):
        d, end = _d(h["check_in"]), _d(h["check_out"])
        while d < end:
            nights.setdefault(d, []).append(h["id"])
            d += timedelta(days=1)
    problems, d = [], _d(span[0])
    while d < _d(span[1]):
        n = nights.get(d, [])
        if not n:
            problems.append(f"no hotel on the night of {d}")
        elif len(n) > 1:
            problems.append(f"{len(n)} hotels on the night of {d} ({', '.join(n)})")
        d += timedelta(days=1)
    for d2, ids in nights.items():
        if not (_d(span[0]) <= d2 < _d(span[1])):
            problems.append(f"hotel night {d2} is outside the trip ({', '.join(ids)})")
    issues = [Issue(rule="R-05 no_overlap", severity=sev, why=p) for p in problems]
    return Check(rule="R-05 no_overlap", ok=not problems, severity=sev,
                 detail="; ".join(problems) or "every night covered once"), issues


def r09_flights(plan, req, items, change, sev):
    out, ret = _flights(items)
    hotels = _hotels(items)
    problems = []
    if not out or not ret:
        if items:
            problems.append("trip needs an outbound and a return flight")
    else:
        if hotels:
            first_in, last_out = min(h["check_in"] for h in hotels), max(h["check_out"] for h in hotels)
            if out["date"] != first_in:
                problems.append(f"outbound {out['date']} but first check-in {first_in}")
            if ret["date"] != last_out:
                problems.append(f"return flight {ret['date']} but last check-out {last_out}")
        if not change:
            if out["date"] != req.start:
                problems.append(f"outbound not on requested start {req.start}")
            if ret["date"] != req.end:
                problems.append(f"return not on requested end {req.end}")
    issues = [Issue(rule="R-09 flights_match_hotels", severity=sev, why=p,
                    slot="flight_ret" if "return" in p else "flight_out") for p in problems]
    return Check(rule="R-09 flights_match_hotels", ok=not problems, severity=sev,
                 detail="; ".join(problems) or "flights line up with hotel stays"), issues


def r04_events(plan, req, items, events, span, sev):
    problems = []
    for e in events:
        ed = _d(e["date"])
        here = any(h.get("city") == e["city"] and _d(h["check_in"]) <= ed < _d(h["check_out"])
                   for h in _hotels(items))
        if not here:
            problems.append(f"not in {e['city']} on {e['date']} for {e['name']}")
    issues = [Issue(rule="R-04 events_covered", severity=sev, why=p) for p in problems]
    return Check(rule="R-04 events_covered", ok=not problems, severity=sev,
                 detail="; ".join(problems) or "all events covered"), issues


def r12_refunds(plan, booked):
    issues = []
    for a in plan.actions:
        if a.op != "cancel" or a.id not in booked:
            continue
        actual, expected = policy.refund_for(booked[a.id]), -a.cost_usd
        if abs(actual - expected) > 0.01:
            issues.append(Issue(rule="R-12 refund_matches_policy", severity="hard",
                                why=f"{a.id}: plan expects ${expected:,.0f} back but the vendor policy refunds "
                                    f"${actual:,.0f}"))
    n = sum(1 for a in plan.actions if a.op == "cancel")
    return Check(rule="R-12 refund_matches_policy", ok=not issues, severity="hard",
                 detail=f"{n} cancel(s) checked"), issues


def r13_change_price(plan, booked):
    issues = []
    for a in plan.actions:
        if a.op != "modify" or a.id not in booked:
            continue
        b = booked[a.id]
        try:
            if b["kind"] == "hotel":
                nights = policy._nights(a.params.get("new_check_in", b["check_in"]), a.params["new_check_out"])
                expected = max(0.0, nights * b["nightly_usd"] - b["cost_usd"])
            elif b["kind"] == "flight":
                new = policy.candidate(a.params["new_flight"])
                expected = policy.FLIGHT_CHANGE_FEE_USD + max(0.0, new["price_usd"] - b["cost_usd"])
            else:
                continue
        except (KeyError, ValueError):
            continue  # R-14 reports bad targets
        if abs(expected - a.cost_usd) > 0.01:
            issues.append(Issue(rule="R-13 change_price_matches", severity="hard",
                                why=f"{a.id}: plan charges ${a.cost_usd:,.0f} but vendor policy price is "
                                    f"${expected:,.0f}"))
    n = sum(1 for a in plan.actions if a.op == "modify")
    return Check(rule="R-13 change_price_matches", ok=not issues, severity="hard",
                 detail=f"{n} change(s) checked"), issues


def r14_targets(plan, booked):
    issues = []
    for a in plan.actions:
        if a.op in ("cancel", "modify") and a.id not in booked:
            issues.append(Issue(rule="R-14 targets_exist", severity="hard",
                                why=f"{a.op} {a.id}: no active booking with that ID on trip {plan.trip_id}"))
        if a.op == "book":
            try:
                policy.candidate(a.id)
            except KeyError:
                issues.append(Issue(rule="R-14 targets_exist", severity="hard", why=f"book {a.id}: unknown item"))
        if a.op == "modify" and a.id in booked and booked[a.id]["kind"] == "flight":
            try:
                policy.candidate(a.params.get("new_flight", ""))
            except KeyError:
                issues.append(Issue(rule="R-14 targets_exist", severity="hard",
                                    why=f"modify {a.id}: unknown new flight {a.params.get('new_flight')}"))
    return Check(rule="R-14 targets_exist", ok=not issues, severity="hard",
                 detail=f"{len(plan.actions)} action(s) checked"), issues


def r20_cheaper_equivalent(plan, req):
    from planner.planner import candidate_cost
    issues = []
    for s in plan.slots:
        p = s.pick_detail
        best = None
        for c in s.candidates:
            if c["id"] == s.pick or c["id"] in plan.excluded_ids or candidate_cost(c) > 0.9 * s.cost_usd:
                continue
            if p["kind"] == "flight":
                same = c["date"] == p["date"] and abs(_minutes(c["dep"]) - _minutes(p["dep"])) <= 90
            else:
                same = c.get("area") == p.get("area") and c.get("rating", 0) >= p.get("rating", 0) - 0.2
            if same and (best is None or candidate_cost(c) < candidate_cost(best)):
                best = c
        if best:
            save = s.cost_usd - candidate_cost(best)
            what = (f"${candidate_cost(best):,.0f} vs ${s.cost_usd:,.0f}, same day, departs {best['dep']} vs {p['dep']}"
                    if p["kind"] == "flight"
                    else f"${candidate_cost(best):,.0f} vs ${s.cost_usd:,.0f}, same area, rating {best.get('rating')}")
            issues.append(Issue(slot=s.slot, rule="R-20 cheaper_equivalent", severity="soft",
                                why=what, found=best["id"], saves_usd=round(save, 2)))
    return Check(rule="R-20 cheaper_equivalent", ok=not issues, severity="soft",
                 detail=f"{len(issues)} cheaper equivalent(s) found"), issues


def run_all(plan: PlanPackage, req: TripRequest, events: list[dict]):
    change = is_change(plan)
    items = itinerary(plan)
    span = _span(items, req, change)
    coverage = "soft" if plan.intent == "cancel" else "hard"
    results = [
        r01_budget(plan, req, items, change),
        r05_no_overlap(plan, req, items, span, coverage),
        r09_flights(plan, req, items, change, coverage),
        r04_events(plan, req, items, events, span, coverage),
    ]
    if change:
        booked = active_bookings(plan.trip_id)
        results += [r14_targets(plan, booked), r12_refunds(plan, booked), r13_change_price(plan, booked)]
    else:
        results.append(r20_cheaper_equivalent(plan, req))
    checks = [c for c, _ in results]
    issues = [i for _, iss in results for i in iss]
    return checks, issues
