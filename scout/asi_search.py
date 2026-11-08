"""Fetch.ai ASI:One client: live web search and short structured answers.

Web search goes through the Responses API with the built-in web_search tool:

    POST {ASI_ONE_BASE_URL}/responses
    {"model": "asi1", "input": "...", "tools": [{"type": "web_search"}]}

(The `web_search: true` flag in the docs is rejected by the live chat API with
"Unrecognized request argument supplied: web_search"; the tool form works.)
Each answer is asked for as JSON and parsed here; nothing is trusted until the
caller validates it.
"""
import json
import re
import uuid

import requests

from common import config

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class SearchError(RuntimeError):
    pass


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.ASI_ONE_API_KEY}", "Content-Type": "application/json",
            "x-session-id": str(uuid.uuid4())}


def parse_json(text: str):
    """The JSON value in a model reply: a ```json fence, else the outermost {...} or [...]."""
    text = (text or "").strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise SearchError(f"no JSON in reply: {text[:120]!r}")
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise SearchError(f"bad JSON in reply ({e}): {text[:120]!r}") from e


def _response_text(data: dict) -> str:
    parts = []
    for item in data.get("output") or []:
        if item.get("type") == "message":
            parts += [c.get("text", "") for c in item.get("content") or [] if c.get("type") == "output_text"]
    return "".join(parts)


def web_search_json(prompt: str) -> tuple[object, dict]:
    """Ask ASI:One to search the web and answer as JSON. Returns (value, info);
    info has input_tokens, output_tokens and the queries it searched."""
    try:
        r = requests.post(f"{config.ASI_ONE_BASE_URL}/responses", headers=_headers(), timeout=config.SEARCH_TIMEOUT_S,
                          json={"model": config.ASI_ONE_MODEL, "input": prompt, "tools": [{"type": "web_search"}]})
    except requests.RequestException as e:
        raise SearchError(f"ASI:One unreachable: {type(e).__name__}") from e
    if not r.ok:
        raise SearchError(f"ASI:One HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    usage = data.get("usage") or {}
    queries = [i["action"].get("query") for i in data.get("output") or []
               if i.get("type") == "web_search_call" and i.get("action")]
    info = {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
            "queries": [q for q in queries if q]}
    return parse_json(_response_text(data)), info


def ask_json(prompt: str, timeout: float = 30) -> tuple[object, dict]:
    """A quick structured answer with no web search (e.g. pull the destination out of a message)."""
    try:
        r = requests.post(f"{config.ASI_ONE_BASE_URL}/chat/completions", headers=_headers(), timeout=timeout,
                          json={"model": config.ASI_ONE_MODEL, "temperature": 0,
                                "messages": [{"role": "user", "content": prompt}]})
    except requests.RequestException as e:
        raise SearchError(f"ASI:One unreachable: {type(e).__name__}") from e
    if not r.ok:
        raise SearchError(f"ASI:One HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    usage = data.get("usage") or {}
    text = data["choices"][0]["message"].get("content") or ""
    return parse_json(text), {"input_tokens": usage.get("prompt_tokens", 0),
                              "output_tokens": usage.get("completion_tokens", 0)}
