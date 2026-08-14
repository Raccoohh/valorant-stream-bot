"""Redis connection lifecycle.

A single shared client is created at startup and closed at shutdown.
redis.asyncio clients are safe to share across tasks — the client
manages an internal connection pool.
"""

from redis.asyncio import Redis

from app.core.config import get_settings

settings = get_settings()

_client: Redis | None = None


def get_redis() -> Redis:
    """Return the shared Redis client. Raises if called before startup."""
    if _client is None:
        raise RuntimeError("Redis client is not initialized — call init_redis() first.")
    return _client


async def init_redis() -> Redis:
    global _client
    _client = Redis.from_url(settings.redis_url, decode_responses=True)
    await _client.ping()
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
