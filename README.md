# TripCheck

**A travel agent you can trust with your money.** You chat in ASI:One. SubQ Snap structures your request and plans the trip. Gemini 4 Argon independently verifies the plan, for at most 3 loops. You approve. The locker room pays only when new money goes out, and Fetch.ai agents execute. Every step is timed and costed.

AIE Code Summit Hackathon, Nov 7, 2026 · Track: Trust but Verify

- **Spec (start here):** [`SPEC.md`](SPEC.md)
- **Visual system design / pitch:** [`docs/tripcheck-build-spec.html`](docs/tripcheck-build-spec.html) (open in a browser)

## Run it now (no keys needed)

```bash
pip install -r requirements.txt
python run_local.py
```

This runs the whole flow on mock data with an in-memory MongoDB:

```
⚡ ready → ⚡ check v1 → ⚡ replan v2 → ⚡ check v2 → ⚡ pass v2
What the verifier caught:
- v1 R-20 cheaper_equivalent: $800 vs $900, same day, departs 11:40 vs 11:00
- v1 R-20 cheaper_equivalent: $60 vs $120, same area, rating 4.5
→ approve → ⚡ pay → ⚡ execute → booked
```

To see the worst case (3 loops, then approval blocked), run `python run_local.py "Japan Nov 15-22, Tokyo then Osaka, budget $1,500"`.

Copy `.env.example` to `.env` and fill in a key to switch that part to the real API. Empty keys stay on the mock.

## Who owns what

| Person | Owns | Files | Replace the mock with |
|---|---|---|---|
| **P1** | ASI:One agent, ① Intake, ② Planner, the loop | `orchestrator/`, `intake/`, `planner/` | real SubQ Snap calls (`intake/subq_client.py`), then run `python -m orchestrator.agent` and test "Chat with Agent" |
| **P2** | ③ Verifier, ④ Human gate | `verifier/`, `gate/` | Gemini 4 Argon review (`verifier/verifier.py`), plus refund rule R-12 |
| **P3** | ⑤ Locker room, ⑥ Fetch agents | `locker/`, `vendors/` | Fetch payment protocol + Stripe **test** (`locker/locker.py`), vendor uAgents (`vendors/agents.py`) |
| Shared | contracts, db, signals, metrics, seed | `common/`, `seed/` | Change only with the team's agreement |

**Rule:** keep your function signatures and the shapes in `common/contracts.py`. Then everyone's parts keep working together.

## Layout

```
common/        contracts.py (all data shapes) · db.py · signals.py · metrics.py · config.py
seed/          seed_japan.py: synthetic Japan trip (flights, hotels, dining, events)
intake/        ① subq_client.py · intake.py
planner/       ② planner.py
verifier/      ③ checks.py (Python rules) · verifier.py (Gemini review + loop decision)
gate/          ④ gate.py (approval message, scoreboard, plan hash)
locker/        ⑤ locker.py (exact-amount payment, hash check)
vendors/       ⑥ vendors.py (book/cancel → ledger) · agents.py (uAgent skeleton)
orchestrator/  pipeline.py (the loop) · agent.py (ASI:One chat agent)
run_local.py   end-to-end run with mocks
```

## Check first (15 min)

- SubQ: call `GET /v1/models` to get the exact Snap model ID, and check the request shape against the [Decisions API docs](https://docs.subq.ai/endpoints/decisions-api/).
- Gemini: confirm your key can call Argon, then set `GEMINI_MODEL`.
- Agentverse: confirm a mailbox agent opens in "Chat with Agent". See the [chat protocol docs](https://docs.agentverse.ai/documentation/getting-started/enable-chat-protocol).

All data is synthetic. Nothing books or charges anything real.
