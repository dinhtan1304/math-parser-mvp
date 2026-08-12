"""
Unit tests for R1 runtime-state primitives in their in-memory fallback mode
(no Redis configured): JWT blacklist, rate limiter, SSE pub/sub bridge.
"""
import asyncio

from starlette.applications import Starlette
from starlette.testclient import TestClient


# ─── Redis soft-dependency ───────────────────────────────────────────────────

def test_redis_client_returns_none_without_url():
    from app.core.redis_client import get_redis
    assert get_redis() is None  # REDIS_URL is empty in tests → in-memory fallback


# ─── R1a: JWT blacklist (in-memory) ──────────────────────────────────────────

def test_blacklist_roundtrip_inmemory():
    from app.api import deps

    async def run():
        token = "header.payload.sig"
        before = await deps.is_token_blacklisted(token)
        await deps.blacklist_token(token, ttl_seconds=60)
        after = await deps.is_token_blacklisted(token)
        other = await deps.is_token_blacklisted("a.different.token")
        return before, after, other

    before, after, other = asyncio.run(run())
    assert before is False
    assert after is True
    assert other is False


# ─── R1a: rate limiter (in-memory) ───────────────────────────────────────────

def test_rate_limiter_blocks_after_limit():
    from app.core.rate_limit import RateLimitMiddleware

    app = Starlette()
    app.add_middleware(RateLimitMiddleware, enabled=True)

    with TestClient(app) as c:
        # /auth/login rule = 5 req/min. Middleware counts before routing,
        # so even unrouted (404) requests are limited.
        codes = [c.post("/api/v1/auth/login").status_code for _ in range(7)]

    assert codes[:5].count(429) == 0, f"first 5 should pass: {codes}"
    assert codes[5] == 429, f"6th request should be limited: {codes}"
    assert codes[6] == 429


def test_rate_limiter_disabled_passthrough():
    from app.core.rate_limit import RateLimitMiddleware

    app = Starlette()
    app.add_middleware(RateLimitMiddleware, enabled=False)
    with TestClient(app) as c:
        codes = [c.post("/api/v1/auth/login").status_code for _ in range(10)]
    assert 429 not in codes  # disabled → never limits


# ─── R1b: SSE pub/sub bridge (in-memory) ─────────────────────────────────────

def test_sse_publish_subscribe_inmemory():
    from app.api import parser as P

    async def run():
        q = await P._subscribe(999)
        # No Redis → in-memory queue, no bridge.
        no_bridge = id(q) not in P._sse_redis_bridges
        registered = q in P._progress_queues.get(999, [])

        P._publish_progress(999, "progress", {"percent": 42, "message": "hi"})
        event, msg = await asyncio.wait_for(q.get(), timeout=2.0)

        await P._unsubscribe(999, q)
        cleaned = 999 not in P._progress_queues
        return no_bridge, registered, event, msg, cleaned

    no_bridge, registered, event, msg, cleaned = asyncio.run(run())
    assert no_bridge is True
    assert registered is True
    assert event == "progress"
    assert '"percent": 42' in msg
    assert cleaned is True


def test_sse_unsubscribe_is_idempotent():
    from app.api import parser as P

    async def run():
        q = await P._subscribe(1234)
        await P._unsubscribe(1234, q)
        await P._unsubscribe(1234, q)  # second call must not raise
        return True

    assert asyncio.run(run()) is True
