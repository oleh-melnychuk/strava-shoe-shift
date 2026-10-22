from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import redis.asyncio as redis

from app import db
from app.models import User
from app.store import (
    create_pending,
    get_pending,
    get_processed,
    get_user_by_strava_athlete_id,
    get_user_by_telegram_id,
    list_preferences,
    record_preference,
    save_processed,
    save_user,
    set_explicit_preference,
)
from app.models import ProcessedActivity


@pytest.fixture
async def redis_client(monkeypatch):
    client = redis.Redis(host="localhost", port=6379, db=15, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis not available on localhost:6379")

    await client.flushdb()
    monkeypatch.setattr(db, "_redis", client)
    yield client
    await client.flushdb()
    await client.aclose()
    monkeypatch.setattr(db, "_redis", None)


@pytest.mark.asyncio
async def test_user_roundtrip(redis_client):
    user = User(telegram_user_id=111, telegram_chat_id=222, strava_athlete_id=333)
    await save_user(user)

    loaded = await get_user_by_telegram_id(111)
    assert loaded is not None
    assert loaded.telegram_chat_id == 222

    by_strava = await get_user_by_strava_athlete_id(333)
    assert by_strava is not None
    assert by_strava.telegram_user_id == 111


@pytest.mark.asyncio
async def test_processed_and_prefs(redis_client):
    await save_processed(
        ProcessedActivity(strava_activity_id=9, user_id=111, status="assigned", run_type="tempo")
    )
    loaded = await get_processed(9)
    assert loaded is not None
    assert loaded.status == "assigned"

    await record_preference(111, "tempo", "g1", "Shoe", accepted=True)
    await record_preference(111, "tempo", "g1", "Shoe", accepted=True)
    prefs = await list_preferences(111)
    assert len(prefs) == 1
    assert prefs[0].accept_count == 2


@pytest.mark.asyncio
async def test_explicit_preference_replaces_same_run_type(redis_client):
    await set_explicit_preference(111, "recovery", "g-asics", "ASICS")
    await set_explicit_preference(111, "recovery", "g-puma", "PUMA")
    prefs = await list_preferences(111)
    recovery = [p for p in prefs if p.run_type == "recovery"]
    assert len(recovery) == 1
    assert recovery[0].shoe_id == "g-puma"


@pytest.mark.asyncio
async def test_pending_ttl(redis_client):
    pending = await create_pending(
        user_id=111,
        strava_activity_id=9,
        run_type="tempo",
        options_json="[]",
        recommended_shoe_id="g1",
        reason="test",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    loaded = await get_pending(pending.id)
    assert loaded is not None
    assert loaded.run_type == "tempo"
