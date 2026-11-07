"""Seed the demo Japan trip: candidates (flights, hotels, dining) and events.

All data is synthetic. `convenience` (0-1) is how close an option is to the
events. The mock planner uses it as a stand-in for what SubQ weighs.
The data is designed so the planner's first pick has a cheaper equivalent
the verifier can catch ($900 → $800 flight, $120 → $60 dinner).

Run:  python -m seed.seed_japan
"""
from common.db import candidates_col, get_db

# Los Angeles demo data. Synthetic businesses (made-up names), real neighbourhoods and
# coordinates, realistic student-budget prices. `safety` is a 0-5 neighbourhood/guest score.
LA_EVENTS = [
    {"id": "EVT-LA1", "name": "Hollywood sightseeing day", "city": "Los Angeles", "date": "2026-11-16"},
]
LA_CANDIDATES = [
    # SFO → LAX, Nov 15
    {"id": "FLT-LA10", "kind": "flight", "slot": "flight_out", "date": "2026-11-15", "from": "SFO",
     "to": "Los Angeles", "dep": "06:00", "airline": "Coastal Air", "price_usd": 129, "convenience": 0.50},
    {"id": "FLT-LA11", "kind": "flight", "slot": "flight_out", "date": "2026-11-15", "from": "SFO",
     "to": "Los Angeles", "dep": "11:00", "airline": "BayJet", "price_usd": 89, "convenience": 0.80},
    {"id": "FLT-LA12", "kind": "flight", "slot": "flight_out", "date": "2026-11-15", "from": "SFO",
     "to": "Los Angeles", "dep": "12:00", "airline": "Coastal Air", "price_usd": 149, "convenience": 0.90},
    # LAX → SFO, Nov 20
    {"id": "FLT-LA20", "kind": "flight", "slot": "flight_ret", "date": "2026-11-20", "from": "Los Angeles",
     "to": "SFO", "dep": "18:00", "airline": "BayJet", "price_usd": 79, "convenience": 0.80},
    {"id": "FLT-LA21", "kind": "flight", "slot": "flight_ret", "date": "2026-11-20", "from": "Los Angeles",
     "to": "SFO", "dep": "10:00", "airline": "Coastal Air", "price_usd": 119, "convenience": 0.60},
    # hotels, Nov 15 → 20 (5 nights)
    {"id": "HTL-LA01", "kind": "hotel", "slot": "hotel_la", "city": "Los Angeles", "area": "Hollywood",
     "name": "Hollywood Budget Inn", "check_in": "2026-11-15", "check_out": "2026-11-20", "nightly_usd": 65,
     "rating": 3.4, "safety": 2.9, "lat": 34.1010, "lon": -118.3380, "convenience": 0.85,
     "policy": {"free_cancel_hours": 24, "penalty_nights": 1}},
    {"id": "HTL-LA02", "kind": "hotel", "slot": "hotel_la", "city": "Los Angeles", "area": "Hollywood",
     "name": "Sunset Student Hostel (private room)", "check_in": "2026-11-15", "check_out": "2026-11-20",
     "nightly_usd": 72, "rating": 4.1, "safety": 4.3, "lat": 34.0980, "lon": -118.3290, "convenience": 0.80,
     "policy": {"free_cancel_hours": 48, "penalty_nights": 1}},
    {"id": "HTL-LA03", "kind": "hotel", "slot": "hotel_la", "city": "Los Angeles", "area": "Koreatown",
     "name": "Koreatown Value Hotel", "check_in": "2026-11-15", "check_out": "2026-11-20", "nightly_usd": 95,
     "rating": 4.2, "safety": 4.0, "lat": 34.0614, "lon": -118.3005, "convenience": 0.60,
     "policy": {"free_cancel_hours": 48, "penalty_nights": 1}},
    {"id": "HTL-LA04", "kind": "hotel", "slot": "hotel_la", "city": "Los Angeles", "area": "Hollywood",
     "name": "Hollywood Boulevard Suites", "check_in": "2026-11-15", "check_out": "2026-11-20",
     "nightly_usd": 210, "rating": 4.6, "safety": 4.6, "lat": 34.1017, "lon": -118.3400, "convenience": 0.95,
     "policy": {"free_cancel_hours": 72, "penalty_nights": 1}},
    # Desi halal restaurants: suggestions shown with the plan (not booked)
    {"id": "RST-LA1", "kind": "restaurant", "city": "Los Angeles", "area": "Hollywood", "cuisine": "Desi halal",
     "name": "Lahore Grill Hollywood", "price": "$", "rating": 4.5, "lat": 34.0983, "lon": -118.3262},
    {"id": "RST-LA2", "kind": "restaurant", "city": "Los Angeles", "area": "Hollywood", "cuisine": "Desi halal",
     "name": "Halal Tandoor Express", "price": "$", "rating": 4.2, "lat": 34.1010, "lon": -118.3440},
    {"id": "RST-LA3", "kind": "restaurant", "city": "Los Angeles", "area": "East Hollywood", "cuisine": "Desi halal",
     "name": "Desi Dhaba Sunset", "price": "$", "rating": 4.4, "lat": 34.0905, "lon": -118.3090},
    {"id": "RST-LA4", "kind": "restaurant", "city": "Los Angeles", "area": "Koreatown", "cuisine": "Desi halal",
     "name": "Karachi Kitchen", "price": "$$", "rating": 4.6, "lat": 34.0600, "lon": -118.3010},
    {"id": "RST-LA5", "kind": "restaurant", "city": "Los Angeles", "area": "Koreatown", "cuisine": "Desi halal",
     "name": "Biryani House LA", "price": "$", "rating": 4.3, "lat": 34.0635, "lon": -118.2920},
]

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
    # later return options, used by reschedule / extend
    {"id": "FLT-2190", "kind": "flight", "slot": "flight_ret", "date": "2026-11-24",
     "from": "Osaka", "to": "SFO", "dep": "13:00", "airline": "Sky Nippon", "price_usd": 870, "convenience": 0.0},
    {"id": "FLT-2195", "kind": "flight", "slot": "flight_ret", "date": "2026-11-25",
     "from": "Osaka", "to": "SFO", "dep": "14:30", "airline": "Sky Nippon", "price_usd": 840, "convenience": 0.0},
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
    for c in CANDIDATES + LA_CANDIDATES:  # upsert by id: safe to run repeatedly against Atlas
        candidates_col().replace_one({"id": c["id"]}, dict(c), upsert=True)
    for e in EVENTS + LA_EVENTS:
        db["events"].replace_one({"id": e["id"]}, dict(e), upsert=True)


if __name__ == "__main__":
    seed()
    print(f"Seeded {len(CANDIDATES)} candidates and {len(EVENTS)} events.")
