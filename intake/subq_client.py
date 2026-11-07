"""Thin client for SubQ Snap's Decisions API (POST /v1/systemone).

VERIFY the request/response shape against
https://docs.subq.ai/endpoints/decisions-api/ before relying on it.
Everything that depends on the exact shape is in this one file.

Snap answers questions; it does not write text. Question types:
  - "choice": pick one of 2-255 labelled options -> choice, probs, confidence
  - "noul":   yes/no -> probability of yes
  - "score":  a score on a legend
Output tokens are always 0 (you pay for input only).
"""
import requests

from common import config


class SubQError(RuntimeError):
    pass


def choice_q(instructions: str, options: dict[str, str | None]) -> dict:
    """Build a choice question. options: {label: description or None}."""
    return {"type": "choice", "instructions": instructions, "criteria": options}


def yes_no_q(instructions: str) -> dict:
    return {"type": "noul", "instructions": instructions}


def decide(state: str, questions: dict[str, dict], images: list | None = None) -> dict:
    """Call SubQ Snap. Returns {"answers": {name: {...}}, "usage": {...}}.

    Normalised answer per question:
      choice -> {"choice": label, "probs": {label: p}, "confidence": c}
      noul   -> {"yes": p}
    """
    if len(questions) > 16:
        raise SubQError("SubQ Snap takes at most 16 questions per call; split the call.")
    body = {"model": config.SUBQ_MODEL, "state": state, "questions": questions}
    if images:
        body["images"] = images[:4]
    r = requests.post(
        f"{config.SUBQ_BASE_URL}/systemone",
        headers={"Authorization": f"Bearer {config.SUBQ_API_KEY}"},
        json=body,
        timeout=60,
    )
    if r.status_code == 402:
        raise SubQError("SubQ returned 402: no credits on this key.")
    if not r.ok:
        raise SubQError(f"SubQ {r.status_code}: {r.text[:500]}")
    data = r.json()
    answers = {}
    for name, a in (data.get("answers") or {}).items():
        # VERIFY: field names below follow the docs as quoted in search results.
        if "choice" in a or a.get("type") == "choice":
            c = a.get("choice") if isinstance(a.get("choice"), (str, int)) else (a.get("choice") or {}).get("choice")
            answers[name] = {
                "choice": c,
                "probs": a.get("probabilities") or a.get("probs") or {},
                "confidence": a.get("confidence", 0.0),
            }
        elif "noul" in a:
            answers[name] = {"yes": a["noul"]}
        else:
            answers[name] = a
    return {"answers": answers, "usage": data.get("usage", {})}


def input_tokens(usage: dict, fallback_text: str = "") -> int:
    for k in ("input_tokens", "prompt_tokens", "total_tokens"):
        if usage.get(k):
            return int(usage[k])
    return max(1, len(fallback_text) // 4)
