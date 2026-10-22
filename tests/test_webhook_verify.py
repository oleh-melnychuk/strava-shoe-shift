from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.strava import webhook as wh


def test_health_route():
    from app.main import health

    mini = FastAPI()
    mini.add_api_route("/health", health, methods=["GET"])
    client = TestClient(mini)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_strava_webhook_verification(monkeypatch):
    class _S:
        strava_verify_token = "test-verify"

    monkeypatch.setattr(wh, "get_settings", lambda: _S())

    mini = FastAPI()
    mini.include_router(wh.router)
    client = TestClient(mini)

    ok = client.get(
        "/strava/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.challenge": "challenge-token",
            "hub.verify_token": "test-verify",
        },
    )
    assert ok.status_code == 200
    assert ok.json() == {"hub.challenge": "challenge-token"}

    bad = client.get(
        "/strava/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.challenge": "challenge-token",
            "hub.verify_token": "wrong",
        },
    )
    assert bad.status_code == 403


def test_strava_webhook_post_force_flag(monkeypatch):
    calls = []

    async def fake_handle(activity_id, athlete_id, *, force=False):
        calls.append((activity_id, athlete_id, force))

    monkeypatch.setattr(wh, "handle_activity_created", fake_handle)

    mini = FastAPI()
    mini.include_router(wh.router)
    client = TestClient(mini)
    payload = {
        "aspect_type": "create",
        "object_type": "activity",
        "object_id": 20689570141,
        "owner_id": 155187767,
        "subscription_id": 378632,
        "updates": {},
    }

    assert client.post("/strava/webhook", json=payload).status_code == 200
    assert calls[-1] == (20689570141, 155187767, False)

    assert client.post("/strava/webhook?force=true", json=payload).status_code == 200
    assert calls[-1] == (20689570141, 155187767, True)

    assert client.post("/strava/webhook", json={**payload, "force": True}).status_code == 200
    assert calls[-1] == (20689570141, 155187767, True)
