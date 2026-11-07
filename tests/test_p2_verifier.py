"""P2 checks: verifier rules on book AND change plans, Gemini output validation,
the loop decision rule, and the human gate. Offline (Gemini mocked).

    python -m tests.test_p2_verifier

Setting: the Japan trip is booked; it's 2026-11-18 10:00 and you're in Tokyo.
"""
import os

os.environ.setdefault("MOCK", "subq,gemini")
os.environ["DEMO_NOW"] = "2026-11-18T10:00:00"
os.environ["MONGODB_URI"] = ""

from common.contracts import Action, PlanPackage, Signal  # noqa: E402
from common.db import clean, plans_col, requests_col, verdicts_col  # noqa: E402
from gate import gate  # noqa: E402
from orchestrator.pipeline import approve_and_execute, plan_trip  # noqa: E402
from seed.seed_japan import seed  # noqa: E402
from vendors import policy  # noqa: E402
from verifier.verifier import decide, run_verifier, validate_gemini_issues  # noqa: E402
from common.contracts import Issue  # noqa: E402

TRIP = "JP-1115"
FAILS = []


def check(cond, what):
    print(f"  {'PASS' if cond else 'FAIL'}  {what}")
    if not cond:
        FAILS.append(what)


def verify(intent: str, actions: list[Action]):
    v = max(p["version"] for p in plans_col().find({"trip_id": TRIP})) + 1
    plan = PlanPackage(trip_id=TRIP, version=v, intent=intent, slots=[], actions=actions,
                       total_usd=policy.net_delta(actions), money_delta_usd=policy.net_delta(actions))
    plans_col().insert_one(plan.model_dump())
    requests_col().update_one({"trip_id": TRIP}, {"$set": {"first_version": v, "intent": intent}})  # as intake would
    sig = run_verifier(Signal(type="check", trip_id=TRIP, version=v))
    verdict = clean(verdicts_col().find_one({"trip_id": TRIP, "version": v}))
    rules = {i["rule"].split()[0]: i["severity"] for i in verdict["issues"]}
    return v, sig, verdict, rules


def main():
    seed()
    _, version = plan_trip(TRIP, "Japan Nov 15-22, Tokyo then Osaka, budget $3,500", trace=lambda *_: None)
    approve_and_execute(TRIP, version, "approve", trace=lambda *_: None)

    print("change plans: correct ones pass")
    v, sig, vd, rules = verify("cancel", [policy.cancel_action(TRIP, "HTL-4471")])
    check(vd["result"] == "PASS" and vd["hard_failures"] == 0, "cancel with policy refund → PASS")
    check(rules.get("R-05") == "soft", "leaving Osaka nights empty is a heads-up, not a block, for a cancel")
    v, sig, vd, rules = verify("extend", [policy.extend_hotel_action(TRIP, "HTL-4471", 2),
                                          policy.change_flight_action(TRIP, "FLT-2101", "FLT-2190")])
    check(vd["result"] == "PASS", "extend hotel to Nov 24 + move flight to Nov 24 → PASS")

    print("change plans: mistakes are caught")
    bad = policy.cancel_action(TRIP, "HTL-4471")
    bad.cost_usd = -480.0
    v, sig, vd, rules = verify("cancel", [bad])
    check(rules.get("R-12") == "hard" and sig.type == "replan", "wrong refund ($480 vs policy $320) → R-12, replan")
    v, sig, vd, rules = verify("extend", [policy.extend_hotel_action(TRIP, "HTL-4471", 2)])
    check(rules.get("R-09") == "hard", "extend hotel but forget the flight → R-09 hard")
    cheap = policy.extend_hotel_action(TRIP, "HTL-4471", 2)
    cheap.cost_usd = 0.0
    v, sig, vd, rules = verify("extend", [cheap, policy.change_flight_action(TRIP, "FLT-2101", "FLT-2190")])
    check(rules.get("R-13") == "hard", "extension priced $0 instead of $320 → R-13 hard")
    v, sig, vd, rules = verify("cancel", [Action(op="cancel", id="HTL-2290", cost_usd=-100)])
    check(rules.get("R-14") == "hard", "cancel a hotel you never booked → R-14 hard")
    v, sig, vd, rules = verify("reschedule", [policy.shift_hotel_action(TRIP, "HTL-4471", 2)])
    check(rules.get("R-05") == "hard" and rules.get("R-09") == "hard",
          "move Osaka 2 days later without fixing Nov 19–20 or the flight → R-05 + R-09 hard")
    v, sig, vd, rules = verify("reschedule", [policy.shift_hotel_action(TRIP, "HTL-4471", 2),
                                              policy.change_flight_action(TRIP, "FLT-2101", "FLT-2190")])
    check("R-05" in rules and "R-09" not in rules, "fix the flight but not the 2-night gap → still R-05")

    print("loop decision rule")
    check(decide([Issue(rule="x", severity="soft", why="note")]) == "PASS", "soft note without a fix → PASS")
    check(decide([Issue(rule="x", severity="soft", why="cheaper", found="FLT-0915")]) == "REJECT",
          "soft issue with a concrete fix → REJECT (worth a loop)")
    check(decide([Issue(rule="x", severity="hard", why="bad")]) == "REJECT", "hard → REJECT")

    print("Gemini output can't send the planner to a made-up option")
    book_plan = PlanPackage(**clean(plans_col().find_one({"trip_id": TRIP, "version": 1})))
    out = validate_gemini_issues(book_plan, [
        {"slot": "flight_out", "why": "cheaper", "found": "FLT-0915", "saves_usd": 100},
        {"slot": "flight_out", "why": "made up", "found": "FLT-7777"},
        {"slot": "hotel_tokyo", "why": "wrong slot", "found": "FLT-0915"},
        {"slot": "nope", "why": "unknown slot", "found": "HTL-2244"},
        {"why": "general remark"},
    ])
    check(out[0].found == "FLT-0915", "real candidate in the same slot kept")
    check(all(i.found is None for i in out[1:]), "made-up / wrong-slot / unknown-slot fixes dropped")
    check(all(i.severity == "soft" for i in out), "Gemini issues are always soft")

    print("human gate")
    v, sig, vd, rules = verify("extend", [policy.extend_hotel_action(TRIP, "HTL-4471", 2),
                                          policy.change_flight_action(TRIP, "FLT-2101", "FLT-2190")])
    msg, shown = gate.build_message(sig)
    check("You pay now: $490" in msg and "change HTL-4471" in msg, "change message shows what changes and $490 to pay")
    check(gate.approve(TRIP, shown, "looks good") is None, "'looks good' is not an approval")
    s1 = gate.approve(TRIP, shown, "Approve")
    check(s1 is not None and s1.type == "pay", "'Approve' → pay signal (new money)")
    check(gate.approve(TRIP, shown, "approve") is None, "approving twice does nothing")
    v, sig, vd, rules = verify("cancel", [policy.cancel_action(TRIP, "DIN-102")])
    msg, shown = gate.build_message(sig)
    check("no card needed" in msg, "cancel message says no card needed")
    s2 = gate.approve(TRIP, shown, "yes approve the cancel")
    check(s2 is not None and s2.type == "execute", "'approve the cancel' → execute without the locker room")
    bad = policy.cancel_action(TRIP, "HTL-2210")
    bad.cost_usd = -999
    v, sig, vd, rules = verify("cancel", [bad])
    check(gate.approve(TRIP, v, "approve") is None, "plan with a hard failure can't be approved")

    print("each request gets its own 3 loops; earlier plans are never overwritten")
    before = plans_col().count_documents({"trip_id": TRIP})
    last = max(p["version"] for p in plans_col().find({"trip_id": TRIP}))
    v1_total = plans_col().find_one({"trip_id": TRIP, "version": 1})["total_usd"]
    msg, shown = plan_trip(TRIP, "Japan Nov 15-22, Tokyo then Osaka, budget $1,500", trace=lambda *_: None)
    new = sorted(p["version"] for p in plans_col().find({"trip_id": TRIP, "version": {"$gt": last}}))
    check(new == [last + 1, last + 2, last + 3], f"new request used v{last + 1}..v{last + 3} (3 loops)")
    check(plans_col().find_one({"trip_id": TRIP, "version": 1})["total_usd"] == v1_total
          and plans_col().count_documents({"trip_id": TRIP}) == before + 3, "original v1 untouched")
    check(shown in new and "best of 3 loops" in msg, "gate picked the best of THIS request's versions")

    print("\nALL PASSED" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    raise SystemExit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
