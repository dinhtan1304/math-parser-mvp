"""Optional Redis client for shared runtime state.

Backs the JWT blacklist, rate-limit counters and SSE one-time tokens with
Redis so they survive restarts and work across multiple workers.

Design: this is a *soft* dependency. When ``REDIS_URL`` is unset, the redis
library is missing, or the server is unreachable, ``get_redis()`` returns
``None`` and every caller transparently falls back to its in-memory path.
That keeps local/SQLite dev and single-worker deploys working unchanged.
"""

import logging
from typing import Optional, Any

from app.core.config import settings

logger = logging.getLogger(__name__)

_redis: Optional[Any] = None  # redis.asyncio.Redis | None


async def init_redis() -> None:
    """Create + ping the Redis client when ``REDIS_URL`` is configured.

    Call once at startup. Any failure degrades silently to in-memory state.
    """
    global _redis
    if not settings.REDIS_URL:
        logger.info(
            "REDIS_URL not set — using in-memory fallback for "
            "blacklist / rate-limit / SSE tokens"
        )
        return
    try:
        import redis.asyncio as aioredis  # lazy import: optional dependency
        client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=3,
        )
        await client.ping()
        _redis = client
        logger.info("Redis connected — shared runtime state enabled")
    except Exception as e:  # ImportError, connection error, auth error, ...
        _redis = None
        logger.warning(
            "Redis unavailable (%s); falling back to in-memory state", e
        )


async def close_redis() -> None:
    """Close the Redis client on shutdown (no-op when in-memory)."""
    global _redis
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception:
            pass
        _redis = None


def get_redis() -> Optional[Any]:
    """Return the shared Redis client, or ``None`` when unavailable.

    Callers MUST handle ``None`` by using their in-memory fallback.
    """
    return _redis
