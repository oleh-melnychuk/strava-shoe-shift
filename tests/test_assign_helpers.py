from app.services.assign_shoe import (
    _assignment_message,
    _is_strava_default_gear,
    _primary_shoe_id,
    _shoe_payload,
    _should_skip_geared,
    _usage_trends,
)


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


def test_strava_default_gear_is_not_skipped():
    shoes = _shoe_payload(
        [
            {"id": "g-default", "name": "Daily", "distance": 1000, "primary": True},
            {"id": "g-tempo", "name": "Tempo", "distance": 1000, "primary": False},
        ]
    )
    primary = _primary_shoe_id(shoes)
    assert primary == "g-default"
    assert _is_strava_default_gear("g-default", primary)
    # Strava auto-tagged default shoe -> bot should reassign, not skip
    assert not _should_skip_geared("g-default", primary, skip_if_geared=True)
    # Manually chosen non-default shoe -> respect it and skip
    assert _should_skip_geared("g-tempo", primary, skip_if_geared=True)
    # No gear / disabled flag / force never skip
    assert not _should_skip_geared(None, primary, skip_if_geared=True)
    assert not _should_skip_geared("g-tempo", primary, skip_if_geared=False)
    assert not _should_skip_geared("g-tempo", primary, skip_if_geared=True, force=True)


def test_assignment_message_shows_change_and_link():
    text = _assignment_message(
        20689570141,
        "Afternoon Run",
        "recovery",
        "Puma Velocity Nitro 4",
        shoe_id="g-puma",
        previous_shoe_name="Adidas Adizero Boston 12",
        previous_shoe_id="g-boston",
        confidence=0.9,
    )
    assert "Shoes changed" in text
    assert "Adidas Adizero Boston 12 → <b>Puma Velocity Nitro 4</b>" in text
    assert "https://www.strava.com/activities/20689570141" in text
