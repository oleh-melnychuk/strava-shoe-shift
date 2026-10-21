from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ai.recommender import ShoeRecommendation
from app.services import assign_shoe


class FakeStrava:
    def __init__(self):
        self.updated = None

    async def get_activity(self, activity_id: int):
        return {
            "id": activity_id,
            "name": "Morning Tempo",
            "type": "Run",
            "distance": 10000,
            "moving_time": 2400,
            "gear_id": None,
            "workout_type": 3,
        }

    async def get_athlete(self):
        return {
            "shoes": [
                {"id": "g-tempo", "name": "Tempo Shoe", "distance": 120000, "retired": False},
                {"id": "g-daily", "name": "Daily", "distance": 500000, "retired": False},
            ]
        }

    async def list_activities(self, page: int = 1, per_page: int = 50):
        return [
            {"type": "Run", "gear_id": "g-tempo", "distance": 10000, "name": "Tempo 1"},
            {"type": "Run", "gear_id": "g-daily", "distance": 8000, "name": "Easy"},
        ]

    async def update_activity_gear(self, activity_id: int, gear_id: str):
        self.updated = (activity_id, gear_id)
        return {"id": activity_id, "gear_id": gear_id}


@pytest.mark.asyncio
async def test_auto_assign_when_confident(monkeypatch):
    fake = FakeStrava()
    user = SimpleNamespace(
        id=1,
        telegram_user_id=1,
        telegram_chat_id=42,
        access_token="tok",
        refresh_token="ref",
        token_expires_at=9999999999,
        skip_if_geared=True,
        strava_athlete_id=99,
    )

    monkeypatch.setattr(assign_shoe, "get_user_by_strava_athlete_id", AsyncMock(return_value=user))
    monkeypatch.setattr(assign_shoe, "get_processed", AsyncMock(return_value=None))
    monkeypatch.setattr(assign_shoe, "get_client_for_user", AsyncMock(return_value=fake))
    monkeypatch.setattr(
        assign_shoe,
        "recommend_shoe",
        AsyncMock(
            return_value=ShoeRecommendation(
                run_type="tempo",
                shoe_id="g-tempo",
                confidence=0.9,
                reason="Looks like tempo; used Tempo Shoe before.",
            )
        ),
    )
    monkeypatch.setattr(assign_shoe, "_mark_processed", AsyncMock())

    sent = {}

    class FakeBot:
        def __init__(self, token: str):
            self.token = token

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            sent["chat_id"] = chat_id
            sent["text"] = text

    monkeypatch.setattr(assign_shoe, "Bot", FakeBot)
    monkeypatch.setattr(
        assign_shoe,
        "get_settings",
        lambda: SimpleNamespace(confidence_threshold=0.75, telegram_bot_token="1:fake"),
    )

    await assign_shoe.handle_activity_created(123, 99)

    assert fake.updated == (123, "g-tempo")
    assert sent["chat_id"] == 42
    assert "tempo" in sent["text"].lower()
