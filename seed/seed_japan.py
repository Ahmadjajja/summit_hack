"""Seed the demo Japan trip: candidates (flights, hotels, dining) and events.

All data is synthetic. `convenience` (0-1) is how close an option is to the
events. The mock planner uses it as a stand-in for what SubQ weighs.
The data is designed so the planner's first pick has a cheaper equivalent
the verifier can catch ($900 → $800 flight, $120 → $60 dinner).

Run:  python -m seed.seed_japan
"""
from common.db import candidates_col, get_db

EVENTS = [
    {"id": "EVT-1", "name": "AI Builders Tokyo", "city": "Tokyo", "date": "2026-11-17"},
    {"id": "EVT-2", "name": "Osaka Dev Summit", "city": "Osaka", "date": "2026-11-20"},
]

CANDIDATES = [
    # outbound SFO → Tokyo, Nov 15
    {"id": "FLT-0882", "kind": "flight", "slot": "flight_out", "date": "2026-11-15",
     "from": "SFO", "to": "Tokyo", "dep": "11:00", "airline": "Pacific Air", "price_usd": 900, "convenience": 0.90},
    {"id": "FLT-0915", "kind": "flight", "slot": "flight_out", "date": "2026-11-15",
     "from": "SFO", "to": "Tokyo", "dep": "11:40", "airline": "Sky Nippon", "price_usd": 800, "convenience": 0.80},
    {"id": "FLT-0950", "kind": "flight", "slot": "flight_out", "date": "2026-11-15",
     "from": "SFO", "to": "Tokyo", "dep": "08:00", "airline": "Pacific Air", "price_usd": 1100, "convenience": 0.50},
    # return Osaka → SFO, Nov 22
    {"id": "FLT-2101", "kind": "flight", "slot": "flight_ret", "date": "2026-11-22",
     "from": "Osaka", "to": "SFO", "dep": "14:00", "airline": "Sky Nippon", "price_usd": 850, "convenience": 0.90},
    {"id": "FLT-2140", "kind": "flight", "slot": "flight_ret", "date": "2026-11-22",
     "from": "Osaka", "to": "SFO", "dep": "10:00", "airline": "Pacific Air", "price_usd": 1000, "convenience": 0.60},
    # Tokyo hotels, Nov 15 → 19 (4 nights)
    {"id": "HTL-2210", "kind": "hotel", "slot": "hotel_tokyo", "city": "Tokyo", "area": "Shinjuku",
     "check_in": "2026-11-15", "check_out": "2026-11-19", "nightly_usd": 180, "rating": 4.4, "convenience": 0.90,
     "policy": {"free_cancel_hours": 72, "penalty_nights": 1}},
    {"id": "HTL-2244", "kind": "hotel", "slot": "hotel_tokyo", "city": "Tokyo", "area": "Shinjuku",
     "check_in": "2026-11-15", "check_out": "2026-11-19", "nightly_usd": 150, "rating": 3.9, "convenience": 0.70,
     "policy": {"free_cancel_hours": 24, "penalty_nights": 1}},
    {"id": "HTL-2290", "kind": "hotel", "slot": "hotel_tokyo", "city": "Tokyo", "area": "Ginza",
     "check_in": "2026-11-15", "check_out": "2026-11-19", "nightly_usd": 240, "rating": 4.7, "convenience": 0.60,
     "policy": {"free_cancel_hours": 48, "penalty_nights": 1}},
    # Osaka hotels, Nov 19 → 22 (3 nights)
    {"id": "HTL-4471", "kind": "hotel", "slot": "hotel_osaka", "city": "Osaka", "area": "Namba",
     "check_in": "2026-11-19", "check_out": "2026-11-22", "nightly_usd": 160, "rating": 4.3, "convenience": 0.90,
     "policy": {"free_cancel_hours": 72, "penalty_nights": 1}},
    {"id": "HTL-4490", "kind": "hotel", "slot": "hotel_osaka", "city": "Osaka", "area": "Umeda",
     "check_in": "2026-11-19", "check_out": "2026-11-22", "nightly_usd": 210, "rating": 4.6, "convenience": 0.70,
     "policy": {"free_cancel_hours": 48, "penalty_nights": 1}},
    # dinner near the Tokyo event, Nov 17
    {"id": "DIN-101", "kind": "dining", "slot": "dinner_tokyo_1117", "city": "Tokyo", "area": "Ginza",
     "date": "2026-11-17", "name": "Omakase next to the venue", "price_usd": 120, "rating": 4.5,
     "distance_mi": 0.1, "convenience": 0.95},
    {"id": "DIN-102", "kind": "dining", "slot": "dinner_tokyo_1117", "city": "Tokyo", "area": "Ginza",
     "date": "2026-11-17", "name": "Omakase one block over", "price_usd": 60, "rating": 4.5,
     "distance_mi": 0.2, "convenience": 0.85},
    {"id": "DIN-103", "kind": "dining", "slot": "dinner_tokyo_1117", "city": "Tokyo", "area": "Shibuya",
     "date": "2026-11-17", "name": "Izakaya", "price_usd": 40, "rating": 4.0,
     "distance_mi": 1.8, "convenience": 0.40},
]


def seed(reset: bool = True) -> None:
    db = get_db()
    if reset:
        db["candidates"].delete_many({})
        db["events"].delete_many({})
    candidates_col().insert_many([dict(c) for c in CANDIDATES])
    db["events"].insert_many([dict(e) for e in EVENTS])


if __name__ == "__main__":
    seed()
    print(f"Seeded {len(CANDIDATES)} candidates and {len(EVENTS)} events.")
