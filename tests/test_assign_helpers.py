from app.services.assign_shoe import _shoe_payload, _usage_trends


def test_shoe_payload_maps_fields():
    shoes = [
        {
            "id": "g1",
            "name": "Vaporfly",
            "distance": 12345,
            "retired": False,
            "primary": True,
        }
    ]
    payload = _shoe_payload(shoes)
    assert payload == [
        {
            "id": "g1",
            "name": "Vaporfly",
            "distance_meters": 12345,
            "distance_km": 12.3,
            "retired": False,
            "primary": True,
        }
    ]


def test_usage_trends_aggregates_runs():
    activities = [
        {"type": "Run", "gear_id": "g1", "distance": 10000, "name": "Easy recovery"},
        {"type": "Run", "gear_id": "g1", "distance": 5000, "name": "B"},
        {"type": "Ride", "gear_id": "b1", "distance": 40000, "name": "Bike"},
        {"type": "Run", "gear_id": None, "distance": 8000, "name": "Bare"},
    ]
    trends = _usage_trends(activities, {"g1": "Daily Trainer"})
    assert len(trends) == 1
    assert trends[0]["shoe_id"] == "g1"
    assert trends[0]["run_count"] == 2
    assert trends[0]["distance_km"] == 15.0
    assert trends[0]["shoe_name"] == "Daily Trainer"
    assert trends[0]["by_run_type"]["recovery"] >= 1
