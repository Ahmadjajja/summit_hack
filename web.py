"""TripCheck web UI: one simple page, real APIs from .env.

    python web.py                 # open http://localhost:8080
    DEMO_NOW=2026-11-18T10:00:00 python web.py   # mid-trip clock for change demos

Standard library only. The page talks to these JSON endpoints:
    POST /api/message  {"text": "..."}   plan (or change) → chat reply
    POST /api/approve                    approve the plan on screen → book/pay/refund
    GET  /api/trip                       bookings, payments, refunds
    POST /api/reset                      start a new trip
    GET  /stripe/return?session_id=...   Stripe Checkout (test mode) sends the traveller back here

With STRIPE_TEST_KEY=sk_test_... and BOOKING_DRY_RUN=false, approving a plan that
needs money opens Stripe's hosted test Checkout (card 4242 4242 4242 4242). The
locker room books only after Stripe confirms that exact amount was paid.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from common import config
from common.contracts import Action, Signal
from common.db import ledger_col, plans_col, verdicts_col
from gate import gate
from locker import locker
from orchestrator import chat_text
from orchestrator.pipeline import execute_approved, plan_trip
from vendors.vendors import precheck
from seed.seed_japan import seed

PAGE = (Path(__file__).parent / "web" / "index.html").read_bytes()
LOCK = threading.Lock()          # one request at a time: it's a single-traveller demo
STATE = {"trip_id": f"WEB-{uuid4().hex[:6].upper()}", "awaiting": None, "checkout": None, "notice": None}


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
            "checkout": ({k: STATE["checkout"][k] for k in ("url", "amount_usd")} if STATE["checkout"] else None),
            "mode": {"subq": "mock" if config.USE_MOCK_SUBQ else config.SUBQ_MODEL,
                     "gemini": "mock" if config.USE_MOCK_GEMINI else config.GEMINI_MODEL,
                     "search": ("off" if not config.LIVE_SEARCH else "mock" if config.USE_MOCK_ASI
                                else f"ASI:One {config.ASI_ONE_MODEL}"),
                     "pay": ("Stripe Checkout (test)" if locker.checkout_enabled() else
                             "Stripe test card" if locker.stripe_enabled() else "mock"),
                     "now": config.DEMO_NOW or "real time"}}


def on_message(text: str) -> dict:
    text = chat_text.clean(text)
    if not text:
        return {"reply": "Type a trip request to start."}
    if chat_text.is_help(text):
        return {"reply": chat_text.HELP}
    if STATE["awaiting"] and text.lower().strip(" .!") in ("approve", "confirm", "book it", "go ahead", "yes"):
        return on_approve()
    if STATE["checkout"]:
        return {"reply": f"Your approved plan is waiting for payment: [pay ${STATE['checkout']['amount_usd']:,.2f} on "
                         f"Stripe (test)]({STATE['checkout']['url']}). Pay, or press **New trip** to start over."}
    trip, steps = STATE["trip_id"], []
    reply, version = plan_trip(trip, text, trace=steps.append)
    verdict = verdicts_col().find_one({"trip_id": trip, "version": version}) or {}
    approvable = verdict.get("hard_failures", 1) == 0
    STATE["awaiting"] = version if approvable else None
    return {"reply": reply, "steps": steps, "can_approve": approvable}


def on_approve() -> dict:
    if not STATE["awaiting"]:
        return {"reply": "There's nothing waiting for approval."}
    trip, version = STATE["trip_id"], STATE["awaiting"]
    plan = plans_col().find_one({"trip_id": trip, "version": version}) or {}
    problems = [f"{a['op']} {a['id']}: {why}" for a in plan.get("actions", [])
                if (why := precheck(trip, Action(**a)))]
    if problems:   # check BEFORE taking any money, not after
        return {"reply": "⚠️ Can't book this plan: " + "; ".join(problems)}
    sig = gate.approve(trip, version, "approve")
    STATE["awaiting"] = None
    if sig is None:
        return {"reply": "That plan can't be approved. Tell me what to change."}
    if sig.type == "pay" and locker.checkout_enabled():
        co = locker.create_checkout(trip, version, f"{config.PUBLIC_URL}/stripe/return")
        STATE["checkout"] = {**co, "signal": sig.model_dump()}
        return {"reply": f"Approved. Pay **${co['amount_usd']:,.2f}** on Stripe's test checkout to book it: "
                         f"[open Stripe Checkout]({co['url']}).\n\nTest mode, no real money: use card "
                         "`4242 4242 4242 4242`, any future date, any CVC. I book as soon as Stripe confirms.",
                "checkout_url": co["url"]}
    return {"reply": execute_approved(sig, trace=lambda *_: None)}


def on_stripe_return(query: dict) -> tuple[str, str]:
    """Stripe sends the traveller here after Checkout. Returns (title, message) for a small page."""
    session_id = (query.get("session_id") or [""])[0]
    co = STATE["checkout"]
    if not co or co["session_id"] != session_id:
        return "Nothing to confirm", "This isn't the payment TripCheck is waiting for. Go back to the TripCheck tab."
    if query.get("cancelled"):
        STATE["notice"] = (f"Payment cancelled on Stripe. Nothing was charged or booked. "
                           f"[Pay ${co['amount_usd']:,.2f} on Stripe (test)]({co['url']}) when you're ready.")
        return "Payment cancelled", "Nothing was charged or booked. Go back to the TripCheck tab."
    try:
        locker.confirm_checkout(session_id)
    except locker.LockerRefused as e:
        return "Not paid yet", f"{e}. Go back to the TripCheck tab."
    STATE["checkout"] = None
    out = execute_approved(Signal(**co["signal"]), trace=lambda *_: None)
    STATE["notice"] = f"💳 Stripe (test mode) confirmed ${co['amount_usd']:,.2f}.\n\n{out}"
    return "Payment received", "Stripe confirmed your test payment and TripCheck booked it. You can close this tab."


RETURN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>TripCheck payment</title>
<style>:root{{--bg:#f4f6f6;--ink:#152022;--ink-2:#55656a}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0e1516;--ink:#e6eeee;--ink-2:#9fb0b3}}}}
body{{background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
display:grid;place-items:center;min-height:100vh;margin:0;padding:16px;box-sizing:border-box}}
main{{max-width:440px}}p{{color:var(--ink-2)}}</style></head>
<body><main><h1>{title}</h1><p>{message}</p></main></body></html>"""


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
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if url.path == "/api/trip":
            with LOCK:
                data = trip_state()
                data["notice"], STATE["notice"] = STATE["notice"], None
            return self._json(data)
        if url.path == "/stripe/return":
            from html import escape
            try:
                with LOCK:
                    title, message = on_stripe_return(parse_qs(url.query))
            except Exception as e:  # show it instead of a blank page
                title, message = "Something went wrong", f"{type(e).__name__}: {e}"
            page = RETURN_PAGE.format(title=escape(title), message=escape(message))
            return self._send(200, page.encode(), "text/html; charset=utf-8")
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
                    STATE.update(trip_id=f"WEB-{uuid4().hex[:6].upper()}", awaiting=None, checkout=None,
                                 notice=None)
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
    mode = trip_state()["mode"]
    print(f"TripCheck UI on http://localhost:{port}  ·  SubQ: {mode['subq']}  ·  Gemini: {mode['gemini']}  ·  "
          f"search: {mode['search']}  ·  pay: {mode['pay']}  ·  now: {config.DEMO_NOW or 'real time'}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
