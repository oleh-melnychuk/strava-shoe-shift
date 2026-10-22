from __future__ import annotations

import json
from datetime import datetime, timezone

from app.db import get_redis
from app.models import PendingDecision, ProcessedActivity, ShoePreference, User

USER_KEY = "user:tg:{telegram_user_id}"
STRAVA_INDEX = "idx:strava:{athlete_id}"
USER_ACTIVITIES = "user:{user_id}:activities"
PROCESSED_KEY = "processed:{activity_id}"
PENDING_KEY = "pending:{pending_id}"
PENDING_SEQ = "pending:seq"
PREF_KEY = "pref:{user_id}:{run_type}:{shoe_id}"
USER_PREFS = "user:{user_id}:prefs"

PENDING_TTL_SECONDS = 24 * 60 * 60


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_user_by_telegram_id(telegram_user_id: int) -> User | None:
    r = await get_redis()
    raw = await r.get(USER_KEY.format(telegram_user_id=telegram_user_id))
    if not raw:
        return None
    return User.from_dict(json.loads(raw))


async def get_user_by_strava_athlete_id(athlete_id: int) -> User | None:
    r = await get_redis()
    telegram_user_id = await r.get(STRAVA_INDEX.format(athlete_id=athlete_id))
    if not telegram_user_id:
        return None
    return await get_user_by_telegram_id(int(telegram_user_id))


async def save_user(user: User) -> User:
    r = await get_redis()
    user.updated_at = _utcnow_iso()
    pipe = r.pipeline()
    pipe.set(USER_KEY.format(telegram_user_id=user.telegram_user_id), json.dumps(user.to_dict()))
    if user.strava_athlete_id is not None:
        pipe.set(STRAVA_INDEX.format(athlete_id=user.strava_athlete_id), str(user.telegram_user_id))
    await pipe.execute()
    return user


async def upsert_telegram_user(telegram_user_id: int, telegram_chat_id: int) -> User:
    user = await get_user_by_telegram_id(telegram_user_id)
    if user:
        user.telegram_chat_id = telegram_chat_id
    else:
        user = User(telegram_user_id=telegram_user_id, telegram_chat_id=telegram_chat_id)
    return await save_user(user)


async def clear_strava_link(athlete_id: int) -> None:
    user = await get_user_by_strava_athlete_id(athlete_id)
    r = await get_redis()
    await r.delete(STRAVA_INDEX.format(athlete_id=athlete_id))
    if not user:
        return
    user.access_token = None
    user.refresh_token = None
    user.token_expires_at = None
    user.strava_athlete_id = None
    await save_user(user)


async def get_processed(activity_id: int) -> ProcessedActivity | None:
    r = await get_redis()
    raw = await r.get(PROCESSED_KEY.format(activity_id=activity_id))
    if not raw:
        return None
    return ProcessedActivity.from_dict(json.loads(raw))


async def clear_processed(activity_id: int, user_id: int | None = None) -> None:
    r = await get_redis()
    pipe = r.pipeline()
    pipe.delete(PROCESSED_KEY.format(activity_id=activity_id))
    if user_id is not None:
        pipe.zrem(USER_ACTIVITIES.format(user_id=user_id), str(activity_id))
    await pipe.execute()


async def save_processed(activity: ProcessedActivity) -> ProcessedActivity:
    r = await get_redis()
    key = PROCESSED_KEY.format(activity_id=activity.strava_activity_id)
    pipe = r.pipeline()
    pipe.set(key, json.dumps(activity.to_dict()))
    pipe.zadd(
        USER_ACTIVITIES.format(user_id=activity.user_id),
        {str(activity.strava_activity_id): datetime.now(timezone.utc).timestamp()},
    )
    await pipe.execute()
    return activity


async def get_last_processed_for_user(user_id: int) -> ProcessedActivity | None:
    r = await get_redis()
    ids = await r.zrevrange(USER_ACTIVITIES.format(user_id=user_id), 0, 0)
    if not ids:
        return None
    return await get_processed(int(ids[0]))


async def create_pending(
    user_id: int,
    strava_activity_id: int,
    run_type: str,
    options_json: str,
    recommended_shoe_id: str | None,
    reason: str | None,
    expires_at: datetime,
) -> PendingDecision:
    r = await get_redis()
    pending_id = int(await r.incr(PENDING_SEQ))
    pending = PendingDecision(
        id=pending_id,
        user_id=user_id,
        strava_activity_id=strava_activity_id,
        run_type=run_type,
        options_json=options_json,
        expires_at=expires_at.isoformat(),
        recommended_shoe_id=recommended_shoe_id,
        reason=reason,
        resolved=False,
    )
    ttl = max(int((expires_at - datetime.now(timezone.utc)).total_seconds()), 1)
    await r.set(PENDING_KEY.format(pending_id=pending_id), json.dumps(pending.to_dict()), ex=ttl)
    return pending


async def get_pending(pending_id: int) -> PendingDecision | None:
    r = await get_redis()
    raw = await r.get(PENDING_KEY.format(pending_id=pending_id))
    if not raw:
        return None
    return PendingDecision.from_dict(json.loads(raw))


async def save_pending(pending: PendingDecision) -> PendingDecision:
    r = await get_redis()
    key = PENDING_KEY.format(pending_id=pending.id)
    ttl = await r.ttl(key)
    payload = json.dumps(pending.to_dict())
    if ttl and ttl > 0:
        await r.set(key, payload, ex=ttl)
    else:
        await r.set(key, payload, ex=PENDING_TTL_SECONDS)
    return pending


async def list_preferences(user_id: int) -> list[ShoePreference]:
    r = await get_redis()
    keys = await r.smembers(USER_PREFS.format(user_id=user_id))
    prefs: list[ShoePreference] = []
    for key in keys:
        raw = await r.get(key)
        if raw:
            prefs.append(ShoePreference.from_dict(json.loads(raw)))
    return prefs


async def clear_preferences(user_id: int) -> int:
    r = await get_redis()
    set_key = USER_PREFS.format(user_id=user_id)
    keys = await r.smembers(set_key)
    if not keys:
        return 0
    pipe = r.pipeline()
    for key in keys:
        pipe.delete(key)
    pipe.delete(set_key)
    await pipe.execute()
    return len(keys)


async def record_preference(
    user_id: int,
    run_type: str,
    shoe_id: str,
    shoe_name: str | None,
    accepted: bool,
    *,
    weight: int = 1,
) -> ShoePreference:
    r = await get_redis()
    key = PREF_KEY.format(user_id=user_id, run_type=run_type, shoe_id=shoe_id)
    raw = await r.get(key)
    if raw:
        pref = ShoePreference.from_dict(json.loads(raw))
    else:
        pref = ShoePreference(user_id=user_id, run_type=run_type, shoe_id=shoe_id)
    if shoe_name:
        pref.shoe_name = shoe_name
    bump = max(int(weight), 1)
    if accepted:
        pref.accept_count += bump
    else:
        pref.reject_count += bump
    pref.updated_at = _utcnow_iso()
    pipe = r.pipeline()
    pipe.set(key, json.dumps(pref.to_dict()))
    pipe.sadd(USER_PREFS.format(user_id=user_id), key)
    await pipe.execute()
    return pref


async def set_explicit_preference(
    user_id: int,
    run_type: str,
    shoe_id: str,
    shoe_name: str | None,
) -> ShoePreference:
    """Pin one shoe for a run type, replacing any previous pin for that type."""
    r = await get_redis()
    set_key = USER_PREFS.format(user_id=user_id)
    existing = await list_preferences(user_id)
    stale = [
        PREF_KEY.format(user_id=user_id, run_type=p.run_type, shoe_id=p.shoe_id)
        for p in existing
        if p.run_type == run_type and p.shoe_id != shoe_id
    ]
    if stale:
        pipe = r.pipeline()
        for key in stale:
            pipe.delete(key)
            pipe.srem(set_key, key)
        await pipe.execute()
    return await record_preference(
        user_id,
        run_type,
        shoe_id,
        shoe_name,
        accepted=True,
        weight=5,
    )
