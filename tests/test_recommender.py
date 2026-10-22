from types import SimpleNamespace

from app.ai import recommender as recommender_module
from app.ai.classifier import classify_heuristic
from app.ai.recommender import (
    ShoeRecommendation,
    _apply_guards,
    _heuristic_from_history,
    _model_candidates,
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


def test_guards_force_explicit_pin_for_recovery():
    shoes = [
        {"id": "g-boston", "name": "Boston"},
        {"id": "g-puma", "name": "Puma"},
    ]
    prefs = [
        {
            "run_type": "recovery",
            "shoe_id": "g-puma",
            "shoe_name": "Puma",
            "accept_count": 5,
            "reject_count": 0,
        }
    ]
    rec = ShoeRecommendation(
        run_type="recovery",
        shoe_id="g-boston",
        confidence=0.9,
        reason="Matched current gear.",
    )
    fixed = _apply_guards(rec, {"gear_id": "g-boston"}, shoes, [], prefs)
    assert fixed.shoe_id == "g-puma"
    assert fixed.confidence >= 0.9


def test_guards_infer_recovery_from_similar_pinned_runs():
    shoes = [
        {"id": "g-boston", "name": "Boston"},
        {"id": "g-puma", "name": "Puma"},
    ]
    prefs = [
        {
            "run_type": "recovery",
            "shoe_id": "g-puma",
            "shoe_name": "Puma",
            "accept_count": 5,
            "reject_count": 0,
        }
    ]
    recent = [
        {"pace_min_per_km": 5.72, "average_heartrate": 153, "distance_km": 7.7, "gear_id": "g-puma"},
        {"pace_min_per_km": 5.82, "average_heartrate": 147, "distance_km": 9.0, "gear_id": "g-puma"},
        {"pace_min_per_km": 5.71, "average_heartrate": 141, "distance_km": 8.0, "gear_id": "g-puma"},
    ]
    activity = {
        "name": "Afternoon Run",
        "distance": 8000,
        "moving_time": 2798,
        "average_heartrate": 148,
        "gear_id": "g-boston",
    }
    rec = ShoeRecommendation(
        run_type="unknown", shoe_id="g-boston", confidence=0.65, reason="Unsure."
    )
    fixed = _apply_guards(rec, activity, shoes, recent, prefs)
    assert fixed.run_type == "recovery"
    assert fixed.shoe_id == "g-puma"
    assert fixed.confidence >= 0.9


def test_build_recent_activity_context_limits_to_50_runs():
    activities = [
        {"id": i, "type": "Run", "name": f"Run {i}", "distance": 5000, "moving_time": 1800, "gear_id": "g1"}
        for i in range(60)
    ]
    activities.append({"id": 99, "type": "Ride", "name": "Bike", "distance": 40000, "gear_id": "b1"})
    rows = build_recent_activity_context(activities, {"g1": "Daily"})
    assert len(rows) == 50
    assert all(r["gear_id"] == "g1" for r in rows)


def test_model_candidates_skip_cooled_down_models(monkeypatch):
    monkeypatch.setattr(recommender_module, "_LAST_GOOD_MODEL", None)
    monkeypatch.setattr(recommender_module, "_MODEL_COOLDOWN", {})
    settings = SimpleNamespace(
        gemini_model="m1",
        gemini_fallback_model="m2",
        gemini_model_cascade="m3,m1",
    )
    assert _model_candidates(settings) == ["m1", "m2", "m3"]
    recommender_module._cool_down_model("m1")
    assert _model_candidates(settings) == ["m2", "m3"]
    # Configured smartest-first order wins over last-good lite models.
    monkeypatch.setattr(recommender_module, "_MODEL_COOLDOWN", {})
    monkeypatch.setattr(recommender_module, "_LAST_GOOD_MODEL", "m3")
    assert _model_candidates(settings) == ["m1", "m2", "m3"]


async def test_weak_lite_answer_is_retried_on_next_model(monkeypatch):
    from types import SimpleNamespace as NS

    settings = NS(
        gemini_api_key="test-key",
        gemini_model="m-smart",
        gemini_fallback_model="m-lite",
        gemini_model_cascade="",
    )
    monkeypatch.setattr(recommender_module, "get_settings", lambda: settings)
    monkeypatch.setattr(recommender_module, "_MODEL_COOLDOWN", {})
    monkeypatch.setattr(recommender_module, "_LAST_GOOD_MODEL", None)
    monkeypatch.setattr(recommender_module.genai, "Client", lambda **kw: object())

    answers = {
        "m-smart": ShoeRecommendation(run_type="unknown", shoe_id="g1", confidence=0.4, reason="not sure"),
        "m-lite": ShoeRecommendation(run_type="tempo", shoe_id="g1", confidence=0.9, reason="clear tempo"),
    }
    calls = []

    async def fake_generate(client, model, contents):
        calls.append(model)
        return answers[model]

    monkeypatch.setattr(recommender_module, "_generate", fake_generate)
    result = await recommend_shoe(
        {"id": 1, "name": "Run", "distance": 10000, "moving_time": 3000, "gear_id": None},
        [{"id": "g1", "name": "Shoe"}],
        [],
        {},
    )
    assert calls == ["m-smart", "m-lite"]
    assert result.run_type == "tempo"
    assert result.confidence == 0.9


async def test_recommend_skips_gemini_when_pin_is_confident(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from app.config import get_settings

    get_settings.cache_clear()

    def forbidden_client(*args, **kwargs):
        raise AssertionError("Gemini must not be called for a confident pin pick")

    monkeypatch.setattr(recommender_module.genai, "Client", forbidden_client)
    monkeypatch.setattr(recommender_module, "_LAST_GOOD_MODEL", None)
    monkeypatch.setattr(recommender_module, "_MODEL_COOLDOWN", {})

    shoes = [
        {"id": "g-boston", "name": "Boston", "distance_meters": 1, "retired": False},
        {"id": "g-puma", "name": "Puma", "distance_meters": 1, "retired": False},
    ]
    recent = [
        {"pace_min_per_km": 5.72, "average_heartrate": 153, "distance_km": 7.7, "gear_id": "g-puma", "inferred_run_type": "recovery"},
        {"pace_min_per_km": 5.82, "average_heartrate": 147, "distance_km": 9.0, "gear_id": "g-puma", "inferred_run_type": "recovery"},
        {"pace_min_per_km": 5.71, "average_heartrate": 141, "distance_km": 8.0, "gear_id": "g-puma", "inferred_run_type": "recovery"},
    ]
    prefs = [
        {
            "run_type": "recovery",
            "shoe_id": "g-puma",
            "shoe_name": "Puma",
            "accept_count": 5,
            "reject_count": 0,
        }
    ]
    result = await recommend_shoe(
        {"name": "Afternoon Run", "distance": 8000, "moving_time": 2798, "average_heartrate": 148},
        shoes,
        recent,
        [],
        prefs,
    )
    assert result.run_type == "recovery"
    assert result.shoe_id == "g-puma"
    assert result.confidence >= 0.85
    get_settings.cache_clear()


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
