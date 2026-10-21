from __future__ import annotations

import redis.asyncio as redis

from app.config import get_settings

_redis: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def init_db() -> None:
    client = await get_redis()
    await client.ping()


async def close_db() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
