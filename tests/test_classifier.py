from app.ai.classifier import activity_summary, normalize_run_type


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
