"""Intake contract tests (offline: mongomock + mock SubQ).

    python -m pytest tests/test_intake.py -q
"""
import pytest

from common import config, db
from common.contracts import Prefs, TripRequest
from intake import intake


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    monkeypatch.setattr(config, "MONGODB_URI", "")
    monkeypatch.setattr(config, "USE_MOCK_SUBQ", True)
    db.get_db.cache_clear()
    intake._indexed_dbs.clear()
    from seed.seed_japan import seed
    seed()
    yield
    db.get_db.cache_clear()


def _req(trip_id) -> TripRequest:
    return TripRequest(**db.clean(db.requests_col().find_one({"trip_id": trip_id})))


def test_new_trip_contract_shape():
    sig = intake.run_intake("T1", "Japan Nov 15-22, Tokyo then Osaka for the dev events, budget $3,500")
    # The ready signal carries the version this request's loop starts at (merged with P2's loop counting).
    assert sig.type == "ready" and sig.trip_id == "T1" and sig.version == 1
    r = _req("T1")
    assert r.intent == "book" and r.cities == ["Tokyo", "Osaka"]  # trip order kept
    assert (r.start, r.end, r.budget_usd) == ("2026-11-15", "2026-11-22", 3500.0)
    assert set(r.events) == {"EVT-1", "EVT-2"}
    assert r.prefs == Prefs(pace="relaxed", dining_style="local")
    assert r.defaults_used == []  # dates, budget and cities were all stated
    # Mongo stores prefs as the same plain object as before the Prefs model
    assert db.requests_col().find_one({"trip_id": "T1"})["prefs"] == {"pace": "relaxed", "dining_style": "local"}


def test_fix1_cancel_keeps_original_trip():
    intake.run_intake("T2", "Japan Nov 16-21, Tokyo then Osaka, budget $4,200")
    db.ledger_col().insert_one({"trip_id": "T2", "type": "booking", "status": "active", "id": "HTL-4471"})
    intake.run_intake("T2", "Cancel my Osaka hotel please")
    r = _req("T2")
    assert r.intent == "cancel" and r.target_booking == "HTL-4471"
    # dates, budget, cities, events come from the original request, not defaults
    assert (r.start, r.end, r.budget_usd) == ("2026-11-16", "2026-11-21", 4200.0)
    assert set(r.cities) == {"Tokyo", "Osaka"} and set(r.events) == {"EVT-1", "EVT-2"}
    assert r.raw_text == "Cancel my Osaka hotel please"  # verifier reviews the latest message
    doc = db.requests_col().find_one({"trip_id": "T2"})
    assert [h["intent"] for h in doc["history"]] == ["book", "cancel"]


def test_fix1_revision_changes_only_what_was_said():
    intake.run_intake("T3", "Japan Nov 15-22, Tokyo then Osaka, budget $3,500")
    intake.run_intake("T3", "actually make the budget $4,000")
    r = _req("T3")
    assert r.budget_usd == 4000.0 and (r.start, r.end) == ("2026-11-15", "2026-11-22")
    assert set(r.cities) == {"Tokyo", "Osaka"}


def test_fix3_change_intent_skips_pref_questions(monkeypatch):
    intake.run_intake("T4", "Japan Nov 15-22, Tokyo, packed schedule, michelin dinners, budget $5,000")
    seen = []
    real = intake._mock

    def spy(text, bookings, questions):
        seen.append(set(questions))
        return real(text, bookings, questions)

    monkeypatch.setattr(intake, "_mock", spy)
    intake.run_intake("T4", "please cancel the whole trip")
    assert seen == [{"intent"}]  # one call, no pace/dining/city questions
    assert _req("T4").prefs == Prefs(pace="packed", dining_style="fine")  # carried over


def test_defaults_used_flags_unstated_values_and_clears_when_stated():
    intake.run_intake("T8", "first time in Kyoto, michelin dinners")  # no dates, no budget
    r = _req("T8")
    assert r.defaults_used == ["budget_usd", "end", "start"]
    assert (r.start, r.end, r.budget_usd) == ("2026-11-15", "2026-11-22", 3500.0)
    intake.run_intake("T8", "budget is $2,000")
    assert _req("T8").defaults_used == ["end", "start"]
    intake.run_intake("T8", "let's do Nov 18 to Nov 21")
    assert _req("T8").defaults_used == []


def test_followup_does_not_overwrite_prefs_with_low_confidence_guesses(monkeypatch):
    intake.run_intake("T10", "Kyoto Nov 18-21, packed schedule, michelin dinners, budget $4,000")
    real = intake._mock

    def unsure(text, bookings, questions):
        a, t = real(text, bookings, questions)
        if "pace" in a:  # live behaviour: SubQ guesses when the message doesn't say
            a["pace"] = {"choice": "relaxed", "confidence": 0.55}
            a["dining_style"] = {"choice": "local", "confidence": 0.3}
        return a, t

    monkeypatch.setattr(intake, "_mock", unsure)
    intake.run_intake("T10", "we have $2,800 for it")
    r = _req("T10")
    assert r.budget_usd == 2800.0 and r.prefs == Prefs(pace="packed", dining_style="fine")
    assert r.intake_confidence["pace"] == 0.7  # original confidence kept, ignored guess (0.55) not stored

    def sure(text, bookings, questions):
        a, t = real(text, bookings, questions)
        if "pace" in a:
            a["pace"] = {"choice": "relaxed", "confidence": 0.99}
        return a, t

    monkeypatch.setattr(intake, "_mock", sure)
    intake.run_intake("T10", "actually let's slow down, relaxed pace")
    assert _req("T10").prefs == Prefs(pace="relaxed", dining_style="fine")


def test_defaults_used_flags_city_fallback():
    intake.run_intake("T9", "Nov 15-22, budget $2,500, somewhere fun")
    r = _req("T9")
    assert r.cities == ["Tokyo"] and r.defaults_used == ["cities"]


def test_old_documents_still_load():
    """Requests written before Prefs/defaults_used existed must still parse."""
    old = {"trip_id": "OLD", "raw_text": "x", "intent": "book", "cities": ["Tokyo"], "start": "2026-11-15",
           "end": "2026-11-22", "budget_usd": 3500.0, "events": [], "prefs": {"pace": "packed"},
           "target_booking": None, "intake_confidence": {}}
    r = TripRequest(**old)
    assert r.prefs == Prefs(pace="packed", dining_style="local") and r.defaults_used == []
    old2 = {**old, "prefs": {}}
    assert TripRequest(**old2).prefs == Prefs()


def test_fix4_unique_trip_index():
    intake.run_intake("T5", "Japan Nov 15-22, Tokyo, budget $3,000")
    names = {i["name"] for i in db.requests_col().list_indexes()}
    assert "uniq_trip_id" in names
    intake.run_intake("T5", "budget $3,100")
    assert db.requests_col().count_documents({"trip_id": "T5"}) == 1


def test_unseeded_city_is_not_swapped_for_a_seeded_one(monkeypatch):
    # live regression: Kyoto (not seeded) came back as Osaka
    real = intake._mock

    def subq_says_osaka(text, bookings, questions):
        a, t = real(text, bookings, questions)
        if "city_osaka" in questions:
            a["city_osaka"] = {"yes": 0.6}
        return a, t

    monkeypatch.setattr(intake, "_mock", subq_says_osaka)
    intake.run_intake("T7", "first time in Kyoto, packed schedule, michelin dinners, Nov 18 to Nov 21")
    assert _req("T7").cities == ["Kyoto"]


def test_fix6_cities_come_from_seeded_data():
    db.candidates_col().insert_one({"id": "HTL-9001", "kind": "hotel", "slot": "hotel_sapporo", "city": "Sapporo",
                                    "check_in": "2026-11-15", "check_out": "2026-11-17", "nightly_usd": 100})
    assert "Sapporo" in intake.known_cities()
    intake.run_intake("T6", "Sapporo Nov 15-17, budget $1,500")
    assert _req("T6").cities == ["Sapporo"]


def test_follow_up_continues_plan_versions():
    """A change request starts after the trip's last plan, so earlier plans are never overwritten."""
    intake.run_intake("T9", "Japan Nov 15-22, Tokyo then Osaka, budget $3,500")
    db.plans_col().insert_many([{"trip_id": "T9", "version": 1}, {"trip_id": "T9", "version": 2}])
    sig = intake.run_intake("T9", "Cancel my Osaka hotel")
    assert sig.version == 3 and _req("T9").first_version == 3
    assert _req("T9").start == "2026-11-15"  # follow-up still merges onto the original trip
