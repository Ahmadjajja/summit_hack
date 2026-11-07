"""TripCheck web UI: one simple page, real APIs from .env.

    python web.py                 # open http://localhost:8080
    DEMO_NOW=2026-11-18T10:00:00 python web.py   # mid-trip clock for change demos

Standard library only. The page talks to these JSON endpoints:
    POST /api/message  {"text": "..."}   plan (or change) → chat reply
    POST /api/approve                    approve the plan on screen → book/pay/refund
    GET  /api/trip                       bookings, payments, refunds
    POST /api/reset                      start a new trip
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from common import config
from common.db import ledger_col, verdicts_col
from orchestrator import chat_text
from orchestrator.pipeline import approve_and_execute, plan_trip
from seed.seed_japan import seed

PAGE = (Path(__file__).parent / "web" / "index.html").read_bytes()
LOCK = threading.Lock()          # one request at a time: it's a single-traveller demo
STATE = {"trip_id": f"WEB-{uuid4().hex[:6].upper()}", "awaiting": None}


def trip_state() -> dict:
    rows = list(ledger_col().find({"trip_id": STATE["trip_id"]}))
    bookings = [{
        "id": b["id"], "kind": b["kind"], "status": b["status"], "cost_usd": b["cost_usd"],
        "label": b.get("name") or b.get("airline") or f"{b.get('area', '')} hotel".strip(),
        "when": (f"{b.get('check_in')} → {b.get('check_out')}" if b["kind"] == "hotel"
                 else f"{b.get('date')} {b.get('dep', '19:00')}".strip()),
        "where": (f"{b.get('from')} → {b.get('to')}" if b["kind"] == "flight" else b.get("city", "")),
    } for b in rows if b["type"] == "booking"]
    paid = sum(r["amount_usd"] for r in rows if r["type"] == "payment" and r.get("status") == "completed")
    refunded = sum(r["amount_usd"] for r in rows if r["type"] == "refund")
    return {"trip_id": STATE["trip_id"], "bookings": bookings, "paid_usd": paid, "refunded_usd": refunded,
            "awaiting_approval": STATE["awaiting"] is not None,
            "mode": {"subq": "mock" if config.USE_MOCK_SUBQ else config.SUBQ_MODEL,
                     "gemini": "mock" if config.USE_MOCK_GEMINI else config.GEMINI_MODEL,
                     "now": config.DEMO_NOW or "real time"}}


def on_message(text: str) -> dict:
    text = chat_text.clean(text)
    if not text:
        return {"reply": "Type a trip request to start."}
    if chat_text.is_help(text):
        return {"reply": chat_text.HELP}
    if STATE["awaiting"] and text.lower().strip(" .!") in ("approve", "confirm", "book it", "go ahead", "yes"):
        return on_approve()
    trip, steps = STATE["trip_id"], []
    reply, version = plan_trip(trip, text, trace=steps.append)
    verdict = verdicts_col().find_one({"trip_id": trip, "version": version}) or {}
    approvable = verdict.get("hard_failures", 1) == 0
    STATE["awaiting"] = version if approvable else None
    return {"reply": reply, "steps": steps, "can_approve": approvable}


def on_approve() -> dict:
    if not STATE["awaiting"]:
        return {"reply": "There's nothing waiting for approval."}
    out = approve_and_execute(STATE["trip_id"], STATE["awaiting"], "approve", trace=lambda *_: None)
    STATE["awaiting"] = None
    return {"reply": out}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, code: int = 200):
        self._send(code, json.dumps(data, default=str).encode())

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/trip":
            return self._json(trip_state())
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        try:
            with LOCK:
                if self.path == "/api/message":
                    res = on_message(str(body.get("text", "")))
                elif self.path == "/api/approve":
                    res = on_approve()
                elif self.path == "/api/reset":
                    STATE.update(trip_id=f"WEB-{uuid4().hex[:6].upper()}", awaiting=None)
                    res = {"reply": "New trip started."}
                else:
                    return self._send(404, b"not found", "text/plain")
                res["trip"] = trip_state()
            self._json(res)
        except Exception as e:  # show the problem in the chat instead of a dead page
            self._json({"reply": f"Something went wrong: {type(e).__name__}: {e}", "error": True,
                        "trip": trip_state()}, 500)

    def log_message(self, fmt, *args):
        pass


def main():
    seed(reset=not config.MONGODB_URI)
    port = int(os.getenv("PORT", "8080"))
    print(f"TripCheck UI on http://localhost:{port}  ·  SubQ: {trip_state()['mode']['subq']}  ·  "
          f"Gemini: {trip_state()['mode']['gemini']}  ·  now: {config.DEMO_NOW or 'real time'}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
