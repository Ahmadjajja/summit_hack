"""④ Human gate (owner: P2). On ⚡ pass or ⚡ max_loops: read the plan, verdicts
and run_log from MongoDB, build the chat message, then handle the reply.

- PASS: show the verified plan; soft issues are listed as heads-ups.
- max_loops: show the BEST version of this request (fewest hard failures, then
  lowest cost). A plan with any hard failure cannot be approved.
- Change plans (cancel / reschedule / extend) show what changes, refunds back to
  the card, and exactly what will be paid now through the locker room.
- Approval stores a hash of the exact plan; the locker room and vendors refuse
  anything that doesn't match it.
"""
import hashlib
import json
import re

from common import config
from common.contracts import PlanPackage, Signal, Verdict
from common.db import clean, plans_col, verdicts_col
from common.metrics import scoreboard

APPROVE_RE = re.compile(r"\b(approve|approved|confirm|book it|go ahead)\b", re.I)


def plan_hash(plan: PlanPackage) -> str:
    payload = json.dumps({"trip_id": plan.trip_id, "version": plan.version,
                          "actions": [a.model_dump() for a in plan.actions],
                          "money_delta_usd": plan.money_delta_usd}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def payable(plan: PlanPackage) -> float:
    return round(sum(a.cost_usd for a in plan.actions if a.op in ("book", "modify") and a.cost_usd > 0), 2)


def refunds(plan: PlanPackage) -> float:
    return round(-sum(a.cost_usd for a in plan.actions if a.op == "cancel"), 2)


def run_versions(signal: Signal, first_version: int | None = None) -> list[int]:
    """Versions that belong to this request's verification loop."""
    if first_version is None:
        from common.db import requests_col
        req = requests_col().find_one({"trip_id": signal.trip_id}) or {}
        first_version = req.get("first_version")
    first = first_version or max(1, signal.version - config.MAX_LOOPS + 1)
    return list(range(first, signal.version + 1))


def choose_version(signal: Signal, first_version: int | None = None) -> int:
    if signal.type == "pass":
        return signal.version
    candidates = []
    for v in run_versions(signal, first_version):
        verdict = verdicts_col().find_one({"trip_id": signal.trip_id, "version": v})
        plan = plans_col().find_one({"trip_id": signal.trip_id, "version": v})
        if verdict and plan:
            candidates.append((verdict["hard_failures"], plan["total_usd"], v))
    return min(candidates)[2]


def _action_line(a) -> str:
    if a.op == "book":
        return f"- book {a.id} · ${a.cost_usd:,.0f}"
    if a.op == "cancel":
        return f"- cancel {a.id} · refund ${-a.cost_usd:,.0f} to your card"
    p = a.params
    what = (f"{p['new_check_in']} → {p['new_check_out']}" if "new_check_out" in p
            else f"to {p.get('new_flight')} on {p.get('new_date')}" if "new_flight" in p else "changed")
    return f"- change {a.id}: {what} · {'$' + format(a.cost_usd, ',.0f') if a.cost_usd > 0 else 'no extra cost'}"


def build_message(signal: Signal, first_version: int | None = None) -> tuple[str, int]:
    versions = run_versions(signal, first_version)
    version = choose_version(signal, first_version)
    plan = PlanPackage(**clean(plans_col().find_one({"trip_id": signal.trip_id, "version": version})))
    verdict = Verdict(**clean(verdicts_col().find_one({"trip_id": signal.trip_id, "version": version})))
    change = plan.intent != "book" or any(a.op != "book" for a in plan.actions)

    status = "verified ✅" if verdict.result == "PASS" else f"best of {len(versions)} loops ⚠️"
    lines = [f"**Trip {plan.trip_id} · {'change' if change else 'plan'} v{version}** ({status})", ""]
    if change:
        lines += [_action_line(a) for a in plan.actions]
        lines += ["", f"**You pay now: ${payable(plan):,.0f}**" + (" (no card needed)" if not payable(plan) else "")]
        if refunds(plan):
            lines.append(f"Refunds to your card: ${refunds(plan):,.0f}")
    else:
        for s in plan.slots:
            d = s.pick_detail
            label = d.get("name") or d.get("airline") or f"{d.get('area')} hotel"
            lines.append(f"- {s.slot}: {label} ({s.pick}) · ${s.cost_usd:,.0f}")
        lines += ["", f"**Total ${plan.total_usd:,.0f}**"]

    first = plans_col().find_one({"trip_id": plan.trip_id, "version": versions[0]})
    if first and version > versions[0]:
        if not change and first["total_usd"] > plan.total_usd:
            lines.append(f"Verifier saved you ${first['total_usd'] - plan.total_usd:,.0f} vs v{versions[0]}.")
        lines += ["", "What the verifier caught:"]
        for v in verdicts_col().find({"trip_id": plan.trip_id, "version": {"$gte": versions[0], "$lt": version}}):
            lines += [f"- v{v['version']} {i['rule']}: {i['why']}" for i in v.get("issues", [])]

    hard = [i for i in verdict.issues if i.severity == "hard"]
    soft = [i for i in verdict.issues if i.severity == "soft"]
    if hard:
        lines += ["", "**Blocking problems:**"] + [f"- {i.rule}: {i.why}" for i in hard]
    if soft:
        lines += ["", "Heads-up:"] + [f"- {i.rule}: {i.why}" for i in soft]
    if verdict.summary:
        lines += ["", f"Verifier's note: {verdict.summary}"]

    lines += ["", "**Scoreboard**", "| loop | SubQ ms | SubQ $ | Gemini ms | Gemini $ |", "|---|---|---|---|---|"]
    for r in scoreboard(plan.trip_id):
        if r["loop"] == "intake" or r["loop"] in versions:
            lines.append(f"| {r['loop']} | {r['subq_ms']} | {r['subq_usd']:.6f} | {r['gemini_ms']} "
                         f"| {r['gemini_usd']:.6f} |")
    if verdict.reviewer:
        lines.append(f"Reviewed by {verdict.reviewer}.")

    if hard:
        lines += ["", "This plan breaks a hard rule, so it can't be approved. Change your request or say 'cancel'."]
    else:
        lines += ["", f"Reply **approve** to {'make this change' if change else 'book it'}."]
    return "\n".join(lines), version


def approve(trip_id: str, version: int, user_text: str) -> Signal | None:
    """Returns ⚡ pay (new money goes out) or ⚡ execute, or None if not approved."""
    if not APPROVE_RE.search(user_text or ""):   # "approve the cancel" is an approval
        return None
    verdict = verdicts_col().find_one({"trip_id": trip_id, "version": version})
    if not verdict or verdict["hard_failures"]:   # never approve an unverified or failing plan
        return None
    doc = plans_col().find_one({"trip_id": trip_id, "version": version})
    if not doc or doc.get("approved"):            # unknown, or already approved (no double execution)
        return None
    plan = PlanPackage(**clean(doc))
    h = plan_hash(plan)
    plans_col().update_one({"trip_id": trip_id, "version": version},
                           {"$set": {"approved": True, "plan_hash": h}})
    if payable(plan) > 0:
        return Signal(type="pay", trip_id=trip_id, version=version, plan_hash=h)
    return Signal(type="execute", trip_id=trip_id, version=version)


def decline(trip_id: str, version: int) -> None:
    plans_col().update_one({"trip_id": trip_id, "version": version}, {"$set": {"declined": True}})
