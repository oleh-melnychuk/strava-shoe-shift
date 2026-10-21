from __future__ import annotations

import time
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.models import User
from app.store import get_user_by_strava_athlete_id as store_get_by_strava
from app.store import get_user_by_telegram_id as store_get_by_telegram
from app.store import save_user
from app.strava.oauth import refresh_access_token

API_BASE = "https://www.strava.com/api/v3"


class StravaClient:
    def __init__(self, access_token: str, settings: Settings | None = None):
        self.access_token = access_token
        self.settings = settings or get_settings()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def get_athlete(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{API_BASE}/athlete", headers=self._headers())
            response.raise_for_status()
            return response.json()

    async def get_activity(self, activity_id: int) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{API_BASE}/activities/{activity_id}",
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()

    async def list_activities(self, page: int = 1, per_page: int = 50) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{API_BASE}/athlete/activities",
                headers=self._headers(),
                params={"page": page, "per_page": per_page},
            )
            response.raise_for_status()
            return response.json()

    async def update_activity_gear(self, activity_id: int, gear_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.put(
                f"{API_BASE}/activities/{activity_id}",
                headers=self._headers(),
                data={"gear_id": gear_id},
            )
            response.raise_for_status()
            return response.json()


async def ensure_fresh_token(user: User, settings: Settings | None = None) -> User:
    settings = settings or get_settings()
    if not user.refresh_token:
        raise ValueError("User has no Strava refresh token")

    now = int(time.time())
    if user.token_expires_at and user.token_expires_at > now + 300 and user.access_token:
        return user

    token_data = await refresh_access_token(settings, user.refresh_token)
    user.access_token = token_data["access_token"]
    user.refresh_token = token_data.get("refresh_token", user.refresh_token)
    user.token_expires_at = token_data["expires_at"]
    return await save_user(user)


async def get_client_for_user(user: User, settings: Settings | None = None) -> StravaClient:
    settings = settings or get_settings()
    user = await ensure_fresh_token(user, settings)
    if not user.access_token:
        raise ValueError("User has no Strava access token")
    return StravaClient(user.access_token, settings)


async def get_user_by_strava_athlete_id(athlete_id: int) -> User | None:
    return await store_get_by_strava(athlete_id)


async def get_user_by_telegram_id(telegram_user_id: int) -> User | None:
    return await store_get_by_telegram(telegram_user_id)
