"""④ Human gate (owner: P2). On ⚡ pass or ⚡ max_loops: read the plan + run_log
from MongoDB, build the chat message with the scoreboard, then handle "approve".

After max loops without a PASS, show the BEST version: fewest hard failures,
then lowest cost. A plan with hard failures cannot be approved.
"""
import hashlib
import json

from common.contracts import PlanPackage, Signal, Verdict
from common.db import clean, plans_col, verdicts_col
from common.metrics import scoreboard


def plan_hash(plan: PlanPackage) -> str:
    payload = json.dumps({"trip_id": plan.trip_id, "version": plan.version,
                          "actions": [a.model_dump() for a in plan.actions],
                          "money_delta_usd": plan.money_delta_usd}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def choose_version(signal: Signal) -> int:
    if signal.type == "pass":
        return signal.version
    best = sorted(verdicts_col().find({"trip_id": signal.trip_id}),
                  key=lambda v: (v["hard_failures"],
                                 plans_col().find_one({"trip_id": signal.trip_id, "version": v["version"]})["total_usd"]))
    return best[0]["version"]


def build_message(signal: Signal) -> tuple[str, int]:
    version = choose_version(signal)
    plan = PlanPackage(**clean(plans_col().find_one({"trip_id": signal.trip_id, "version": version})))
    verdict = Verdict(**clean(verdicts_col().find_one({"trip_id": signal.trip_id, "version": version})))
    first = plans_col().find_one({"trip_id": signal.trip_id, "version": 1})

    lines = [f"**Trip {plan.trip_id} · plan v{version}** "
             f"({'verified ✅' if verdict.result == 'PASS' else f'best of {signal.version} loops ⚠️'})", ""]
    for s in plan.slots:
        d = s.pick_detail
        label = d.get("name") or d.get("airline") or f"{d.get('area')} hotel"
        lines.append(f"- {s.slot}: {label} ({s.pick}) · ${s.cost_usd:,.0f}")
    lines += ["", f"**Total ${plan.total_usd:,.0f}**"]
    if first and version > 1:
        lines.append(f"Verifier saved you ${first['total_usd'] - plan.total_usd:,.0f} vs plan v1.")
        lines += ["", "What the verifier caught:"]
        for v in verdicts_col().find({"trip_id": plan.trip_id, "version": {"$lt": version}}):
            lines += [f"- v{v['version']} {i['rule']}: {i['why']}" for i in v.get("issues", [])]
    if verdict.issues:
        lines += ["", "Open issues:"] + [f"- [{i.severity}] {i.rule}: {i.why}" for i in verdict.issues]

    lines += ["", "**Scoreboard**", "| loop | SubQ ms | SubQ $ | Gemini ms | Gemini $ |", "|---|---|---|---|---|"]
    for r in scoreboard(plan.trip_id):
        lines.append(f"| {r['loop']} | {r['subq_ms']} | {r['subq_usd']:.6f} | {r['gemini_ms']} | {r['gemini_usd']:.6f} |")

    if verdict.hard_failures:
        lines += ["", "This plan breaks a hard rule, so it can't be approved. Change your request or say 'cancel'."]
    else:
        lines += ["", "Reply **approve** to book it."]
    return "\n".join(lines), version


def approve(trip_id: str, version: int, user_text: str) -> Signal | None:
    if "approve" not in user_text.lower():
        return None
    verdict = verdicts_col().find_one({"trip_id": trip_id, "version": version})
    if not verdict or verdict["hard_failures"]:   # never approve an unverified or failing plan
        return None
    plan = PlanPackage(**clean(plans_col().find_one({"trip_id": trip_id, "version": version})))
    h = plan_hash(plan)
    plans_col().update_one({"trip_id": trip_id, "version": version},
                           {"$set": {"approved": True, "plan_hash": h}})
    if any(a.op in ("book", "modify") and a.cost_usd > 0 for a in plan.actions):
        return Signal(type="pay", trip_id=trip_id, version=version, plan_hash=h)
    return Signal(type="execute", trip_id=trip_id, version=version)
