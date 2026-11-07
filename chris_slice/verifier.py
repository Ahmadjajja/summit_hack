"""Gemini verifier gate for TripCheck plans."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types

import db

load_dotenv(override=True)

MODEL = "gemini-3.8-flash"

# Paid-tier standard rates for gemini-3.8-flash through Dec 31, 2026.
# Source: https://ai.google.dev/gemini-api/docs/pricing — update if the page changes.
PRICES = {
    "input_per_1m": 0.75,
    "output_per_1m": 3.75,
}

POLL_SECONDS = 3
MAX_REJECT_LOOPS = 3

VERIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "REJECT"]},
        "reasons": {
            "type": "array",
            "items": {"type": "string"},
        },
        "cheaper_equivalents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "current": {"type": "string"},
                    "cheaper": {"type": "string"},
                    "saving_usd": {"type": "number"},
                },
                "required": ["field", "current", "cheaper", "saving_usd"],
            },
        },
    },
    "required": ["verdict", "reasons", "cheaper_equivalents"],
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _version_key(version: str) -> tuple[int, str]:
    """Sort key so v2 > v1 > v10-as-string edge cases stay sensible."""
    raw = str(version or "")
    if raw.startswith("v") and raw[1:].isdigit():
        return (int(raw[1:]), raw)
    return (-1, raw)


def _estimate_cost_usd(prompt_tokens: int, candidate_tokens: int) -> float:
    return (
        prompt_tokens / 1_000_000.0 * PRICES["input_per_1m"]
        + candidate_tokens / 1_000_000.0 * PRICES["output_per_1m"]
    )


def _newest_planned_per_request() -> list[dict[str, Any]]:
    planned = list(db.plans.find({"status": "planned"}))
    best: dict[str, dict[str, Any]] = {}
    for plan in planned:
        rid = plan.get("request_id")
        if rid is None:
            continue
        current = best.get(rid)
        if current is None or _version_key(plan.get("version", "")) > _version_key(
            current.get("version", "")
        ):
            best[rid] = plan
    return list(best.values())


def _build_prompt(request: dict[str, Any], candidates: list[dict[str, Any]], plan: dict[str, Any]) -> str:
    payload = {
        "request": {
            "request_id": request.get("request_id"),
            "origin": request.get("origin"),
            "destination": request.get("destination"),
            "depart_date": request.get("depart_date"),
            "return_date": request.get("return_date"),
            "party_size": request.get("party_size"),
            "budget_usd": request.get("budget_usd"),
            "notes": request.get("notes"),
        },
        "candidates": [
            {
                "option_id": c.get("option_id"),
                "kind": c.get("kind"),
                "label": c.get("label"),
                "price_usd": c.get("price_usd"),
                "origin": c.get("origin"),
                "destination": c.get("destination"),
                "depart_date": c.get("depart_date"),
                "return_date": c.get("return_date"),
                "party_size": c.get("party_size"),
            }
            for c in candidates
        ],
        "plan": {
            "plan_id": str(plan.get("_id")),
            "version": plan.get("version"),
            "summary": plan.get("summary"),
            "selected_option_ids": plan.get("selected_option_ids"),
            "total_usd": plan.get("total_usd"),
            "origin": plan.get("origin"),
            "destination": plan.get("destination"),
            "depart_date": plan.get("depart_date"),
            "return_date": plan.get("return_date"),
            "party_size": plan.get("party_size"),
            "new_money_usd": plan.get("new_money_usd"),
        },
    }
    return (
        "You are the TripCheck verifier. Check this travel plan against the original "
        "request and the candidate options.\n\n"
        "Rules:\n"
        "1. Does the plan satisfy the request (dates, route, budget, party size)?\n"
        "2. Does the plan use any option that is not in candidates (hallucination)? "
        "If yes, REJECT.\n"
        "3. Can you find cheaper equivalent options among the candidates that keep "
        "the same constraints but lower price? List them in cheaper_equivalents; "
        "do not REJECT solely for cheaper alternatives unless the plan also fails "
        "budget/route/dates/party/hallucination checks.\n"
        "4. If the plan total exceeds budget_usd, REJECT.\n"
        "5. Verdict must be PASS or REJECT.\n\n"
        f"INPUT JSON:\n{json.dumps(payload, default=str)}"
    )


def _parse_verifier_output(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("unparseable") from exc

    verdict = data.get("verdict")
    if verdict not in ("PASS", "REJECT"):
        raise ValueError("unparseable")
    reasons = data.get("reasons")
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise ValueError("unparseable")
    cheaper = data.get("cheaper_equivalents")
    if not isinstance(cheaper, list):
        raise ValueError("unparseable")

    cleaned_cheaper: list[dict[str, Any]] = []
    for item in cheaper:
        if not isinstance(item, dict):
            raise ValueError("unparseable")
        try:
            cleaned_cheaper.append(
                {
                    "field": str(item["field"]),
                    "current": str(item["current"]),
                    "cheaper": str(item["cheaper"]),
                    "saving_usd": float(item["saving_usd"]),
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("unparseable") from exc

    return {
        "verdict": verdict,
        "reasons": reasons,
        "cheaper_equivalents": cleaned_cheaper,
    }


def verify_plan(client: genai.Client, plan: dict[str, Any]) -> dict[str, Any]:
    request_id = plan["request_id"]
    request = db.requests.find_one({"request_id": request_id}) or {}
    candidates = list(db.candidates.find({"request_id": request_id}))
    prompt = _build_prompt(request, candidates, plan)

    started = time.perf_counter()
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=VERIFIER_SCHEMA,
        ),
    )
    latency_ms = int((time.perf_counter() - started) * 1000)

    usage = getattr(response, "usage_metadata", None)
    prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    candidate_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
    cost_usd = _estimate_cost_usd(prompt_tokens, candidate_tokens)

    raw_text = getattr(response, "text", None) or ""
    try:
        parsed = _parse_verifier_output(raw_text)
    except ValueError:
        parsed = {
            "verdict": "REJECT",
            "reasons": ["unparseable verifier output"],
            "cheaper_equivalents": [],
        }

    plan_id = plan["_id"]
    version = plan.get("version", "v1")
    created_at = _utc_now()

    verdict_doc = {
        "plan_id": plan_id,
        "request_id": request_id,
        "version": version,
        "verdict": parsed["verdict"],
        "reasons": parsed["reasons"],
        "cheaper_equivalents": parsed["cheaper_equivalents"],
        "latency_ms": latency_ms,
        "tokens": {
            "prompt_token_count": prompt_tokens,
            "candidates_token_count": candidate_tokens,
        },
        "cost_usd": cost_usd,
        "created_at": created_at,
    }
    db.verdicts.insert_one(verdict_doc)

    db.run_log.insert_one(
        {
            "stage": "verify",
            "plan_id": plan_id,
            "request_id": request_id,
            "version": version,
            "verdict": parsed["verdict"],
            "latency_ms": latency_ms,
            "cost_usd": cost_usd,
            "created_at": created_at,
        }
    )

    if parsed["verdict"] == "PASS":
        db.plans.update_one({"_id": plan_id}, {"$set": {"status": "verified"}})
        new_status = "verified"
    else:
        new_loop = int(plan.get("loop") or 0) + 1
        if new_loop >= MAX_REJECT_LOOPS:
            db.plans.update_one(
                {"_id": plan_id},
                {"$set": {"status": "escalate_human", "loop": new_loop}},
            )
            new_status = "escalate_human"
        else:
            db.plans.update_one(
                {"_id": plan_id},
                {"$set": {"status": "needs_revision", "loop": new_loop}},
            )
            new_status = "needs_revision"

    print(
        f"[verify] plan={plan_id} version={version} verdict={parsed['verdict']} "
        f"status->{new_status} latency_ms={latency_ms} cost_usd={cost_usd:.6f}"
    )
    for reason in parsed["reasons"]:
        print(f"  - {reason}")
    return verdict_doc


def main(once: bool = False) -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing. Copy .env.example to .env and fill it in.")

    client = genai.Client(api_key=api_key)
    mode = "once" if once else f"every {POLL_SECONDS}s"
    print(f"TripCheck verifier polling {mode} (model={MODEL})")

    while True:
        plans = _newest_planned_per_request()
        if not plans:
            if once:
                print("No planned plans to verify.")
                return
            print("… no planned plans; waiting")
        for plan in plans:
            verify_plan(client, plan)
        if once:
            return
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    import sys

    main(once="--once" in sys.argv)
