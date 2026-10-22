from app.ai.classifier import (
    activity_summary,
    classify_by_user_distance,
    classify_heuristic,
    format_distance_profile,
    next_onboarding_run_type,
    normalize_run_type,
)


def test_normalize_run_type():
    assert normalize_run_type("Tempo") == "tempo"
    assert normalize_run_type("junk") == "unknown"
    assert normalize_run_type(None) == "unknown"


def test_activity_summary():
    summary = activity_summary(
        {
            "name": "AM Run",
            "sport_type": "Run",
            "distance": 1000,
            "moving_time": 300,
        }
    )
    assert summary["name"] == "AM Run"
    assert summary["type"] == "Run"
    assert summary["distance_m"] == 1000


def test_next_onboarding_run_type():
    assert next_onboarding_run_type("recovery") == "tempo"
    assert next_onboarding_run_type("race") is None


def test_format_distance_profile():
    assert format_distance_profile({"min_km": None, "max_km": None}) == "any distance"
    assert format_distance_profile({"min_km": 5.0, "max_km": 8.0}) == "5–8 km"
    assert format_distance_profile({"min_km": 22.0, "max_km": None}) == "22 km +"
    assert format_distance_profile({"min_km": 0.0, "max_km": 5.0}) == "under 5 km"


def test_classify_by_user_distance_picks_closest_typical():
    profiles = [
        {"run_type": "recovery", "min_km": 5.0, "max_km": 8.0, "typical_km": 6.5},
        {"run_type": "tempo", "min_km": 8.0, "max_km": 12.0, "typical_km": 10.0},
        {"run_type": "long", "min_km": 16.0, "max_km": 22.0, "typical_km": 19.0},
    ]
    assert classify_by_user_distance(8.0, profiles) == "recovery"
    assert classify_by_user_distance(10.0, profiles) == "tempo"
    assert classify_by_user_distance(18.0, profiles) == "long"
    assert classify_by_user_distance(14.0, profiles) is None
    assert classify_by_user_distance(8.0, [{"run_type": "recovery", "min_km": None, "max_km": None}]) is None


def test_classify_heuristic_uses_user_distances_for_unnamed_runs():
    profiles = [
        {"run_type": "recovery", "min_km": 5.0, "max_km": 8.0, "typical_km": 6.5},
        {"run_type": "long", "min_km": 16.0, "max_km": 22.0, "typical_km": 19.0},
    ]
    assert classify_heuristic({"name": "Afternoon Run", "distance": 8000}, profiles) == "recovery"
    assert classify_heuristic({"name": "Sunday", "distance": 18000}, profiles) == "long"
    # Name cues still win over distance.
    assert classify_heuristic({"name": "Tuesday Tempo", "distance": 8000}, profiles) == "tempo"
