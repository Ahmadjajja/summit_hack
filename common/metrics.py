"""Time + cost tracking. Every model call is wrapped in `track(...)` and
writes one run_log entry. The scoreboard is a sum over run_log."""
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from common import config
from common.contracts import RunLog
from common.db import run_log_col


def cost_for(model: str, input_tokens: int, output_tokens: int) -> float:
    m = model.lower()
    if "subq" in m:
        return input_tokens / 1e6 * config.SUBQ_PRICE_IN_PER_M  # output is free
    if "gemini" in m:
        return (input_tokens / 1e6 * config.GEMINI_PRICE_IN_PER_M
                + output_tokens / 1e6 * config.GEMINI_PRICE_OUT_PER_M)
    if m.startswith("asi1"):
        return (input_tokens / 1e6 * config.ASI_PRICE_IN_PER_M
                + output_tokens / 1e6 * config.ASI_PRICE_OUT_PER_M)
    return 0.0


def estimate_tokens(text: str) -> int:
    """Rough estimate for mock mode. Real calls use the API's usage numbers."""
    return max(1, len(text) // 4)


class _Meter:
    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.note = ""

    def tokens(self, input_tokens: int, output_tokens: int = 0):
        self.input_tokens += int(input_tokens or 0)
        self.output_tokens += int(output_tokens or 0)


@contextmanager
def track(trip_id: str, step: str, model: str, loop: int = 0, version=None):
    meter = _Meter()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.perf_counter()
    try:
        yield meter
    finally:
        ms = int((time.perf_counter() - t0) * 1000)
        entry = RunLog(
            trip_id=trip_id, step=step, loop=loop, version=version, model=model,
            started_at=started, ms=ms,
            input_tokens=meter.input_tokens, output_tokens=meter.output_tokens,
            cost_usd=round(cost_for(model, meter.input_tokens, meter.output_tokens), 6),
            note=meter.note,
        )
        run_log_col().insert_one(entry.model_dump())


def scoreboard(trip_id: str) -> list[dict]:
    """One row per loop: SubQ time/cost, Gemini time/cost."""
    rows = {}
    for e in run_log_col().find({"trip_id": trip_id}):
        key = "intake" if e["step"] == "intake" else e.get("loop", 0)
        if e["step"] not in ("intake", "planner", "verifier"):
            continue
        r = rows.setdefault(key, {"loop": key, "subq_ms": 0, "subq_usd": 0.0,
                                  "gemini_ms": 0, "gemini_usd": 0.0})
        side = "subq" if e["step"] in ("intake", "planner") else "gemini"
        r[f"{side}_ms"] += e["ms"]
        r[f"{side}_usd"] += e["cost_usd"]
    ordered = [rows.pop("intake")] if "intake" in rows else []
    ordered += [rows[k] for k in sorted(rows)]
    return ordered
