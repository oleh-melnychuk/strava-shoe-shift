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
    assert ok.text == "challenge-token"

    bad = client.get(
        "/strava/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.challenge": "challenge-token",
            "hub.verify_token": "wrong",
        },
    )
    assert bad.status_code == 403
