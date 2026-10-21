from app.ai.classifier import classify_heuristic
from app.ai.recommender import (
    ShoeRecommendation,
    _apply_guards,
    _heuristic_from_history,
    build_recent_activity_context,
    recommend_shoe,
)


def test_classify_tempo_from_name():
    assert classify_heuristic({"name": "Tuesday Tempo", "distance": 10000}) == "tempo"


def test_classify_long_from_distance():
    assert classify_heuristic({"name": "Sunday", "distance": 22000}) == "long"


def test_classify_intervals():
    assert classify_heuristic({"name": "Track intervals", "distance": 8000}) == "intervals"


def test_history_heuristic_uses_same_type_shoe():
    shoes = [
        {"id": "g-boston", "name": "Boston", "distance_meters": 100000, "retired": False},
        {"id": "g-puma", "name": "Puma", "distance_meters": 80000, "retired": False},
    ]
    recent = [
        {"inferred_run_type": "recovery", "gear_id": "g-puma"},
        {"inferred_run_type": "recovery", "gear_id": "g-puma"},
        {"inferred_run_type": "tempo", "gear_id": "g-boston"},
    ]
    result = _heuristic_from_history(
        {"name": "Easy recovery", "distance": 5000},
        shoes,
        recent,
    )
    assert result.run_type == "recovery"
    assert result.shoe_id == "g-puma"
    assert result.confidence >= 0.55


def test_guards_do_not_force_prefs():
    shoes = [
        {"id": "g-boston", "name": "Boston"},
        {"id": "g-puma", "name": "Puma"},
    ]
    rec = ShoeRecommendation(
        run_type="recovery",
        shoe_id="g-puma",
        confidence=0.8,
        reason="History says Puma.",
    )
    fixed = _apply_guards(rec, {"gear_id": "g-boston"}, shoes)
    assert fixed.shoe_id == "g-puma"


def test_build_recent_activity_context_limits_to_20_runs():
    activities = [
        {"id": i, "type": "Run", "name": f"Run {i}", "distance": 5000, "moving_time": 1800, "gear_id": "g1"}
        for i in range(25)
    ]
    activities.append({"id": 99, "type": "Ride", "name": "Bike", "distance": 40000, "gear_id": "b1"})
    rows = build_recent_activity_context(activities, {"g1": "Daily"}, limit=20)
    assert len(rows) == 20
    assert all(r["gear_id"] == "g1" for r in rows)


async def test_recommend_without_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    from app.config import get_settings

    get_settings.cache_clear()

    shoes = [{"id": "g1", "name": "Shoe", "distance_meters": 1000, "retired": False}]
    result = await recommend_shoe(
        {"name": "Easy recovery", "distance": 5000},
        shoes,
        [{"inferred_run_type": "recovery", "gear_id": "g1"}],
        [],
    )
    assert result.shoe_id == "g1"
    assert result.run_type == "recovery"

    get_settings.cache_clear()
