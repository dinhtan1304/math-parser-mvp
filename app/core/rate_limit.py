"""
In-memory rate limiter middleware.

Lightweight, zero-dependency approach for MVP.
Uses sliding window per (client_ip, path_prefix).

Rate limits:
  - /api/v1/auth/login:     5/min   (brute-force protection)
  - /api/v1/auth/register:  3/min   (spam protection)
  - /api/v1/parser/parse:   10/min  (costs Gemini API $$$)
  - /api/v1/generate:       10/min  (costs Gemini API $$$)
  - /api/v1/chat:           20/min  (costs Gemini API $$$)
  - Default API:            60/min

Security note:
  X-Forwarded-For is only trusted when TRUST_PROXY=true in settings
  (i.e. server is behind a known reverse proxy). Otherwise direct
  client IP is used to prevent IP-spoofing bypass of rate limits.

For production at scale, replace with Redis-backed limiter or API gateway.
"""

import time
import logging
from collections import defaultdict
from typing import Dict, List, Tuple

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import settings

logger = logging.getLogger(__name__)


# ── Rate limit rules: (path_prefix, max_requests, window_seconds) ──
RATE_RULES: List[Tuple[str, int, int]] = [
    ("/api/v1/auth/login",    5,  60),    # 5 req/min  — brute-force protection
    ("/api/v1/auth/register", 1,  60),    # 1 req/min  — spam protection
    ("/api/v1/parser/parse",  10, 60),    # 10 req/min — Gemini cost
    ("/api/v1/generate",      10, 60),    # 10 req/min — Gemini cost
    ("/api/v1/chat",          20, 60),    # 20 req/min — Gemini cost
]

DEFAULT_LIMIT  = 60   # 60 req/min for all other API endpoints
DEFAULT_WINDOW = 60   # 60 seconds

# Safety cap: if the dict grows beyond this many keys (unique IPs × rules),
# evict the oldest 20% to prevent memory exhaustion under fake-IP flood.
MAX_DICT_KEYS = 50_000


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window in-memory rate limiter."""

    def __init__(self, app, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled
        # Key: "client_ip:rule_prefix" → List of timestamps
        self._requests: Dict[str, List[float]] = defaultdict(list)
        self._last_cleanup = time.time()
        self._cleanup_interval = 60  # Cleanup every 60 s (was 300 s)

    def _get_client_ip(self, request: Request) -> str:
        """
        Get real client IP.

        TRUST_PROXY=false (default): always use the TCP-level peer address.
            → Prevents X-Forwarded-For spoofing on a directly-exposed server.

        TRUST_PROXY=true: trust the leftmost IP in X-Forwarded-For.
            → Use only when behind a trusted reverse proxy (nginx/Railway/etc.)
              that overwrites / prepends the header reliably.
        """
        if settings.TRUST_PROXY:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                return forwarded.split(",")[0].strip()

        return request.client.host if request.client else "unknown"

    def _find_rule(self, path: str) -> Tuple[str, int, int]:
        """Find matching rate limit rule for path."""
        for prefix, limit, window in RATE_RULES:
            if path.startswith(prefix):
                return prefix, limit, window
        if path.startswith("/api/"):
            return "/api/", DEFAULT_LIMIT, DEFAULT_WINDOW
        return "", 0, 0  # No limit for non-API paths (HTML, static)

    def _cleanup_old_entries(self):
        """Remove expired entries to prevent memory leak."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return

        self._last_cleanup = now
        cutoff = now - max(DEFAULT_WINDOW, 60)
        keys_to_delete = []

        for key, timestamps in self._requests.items():
            self._requests[key] = [t for t in timestamps if t > cutoff]
            if not self._requests[key]:
                keys_to_delete.append(key)

        for key in keys_to_delete:
            del self._requests[key]

        # Safety cap: evict oldest 20% if dict is still too large after cleanup
        if len(self._requests) > MAX_DICT_KEYS:
            overflow = len(self._requests) - MAX_DICT_KEYS
            evict_count = max(overflow, MAX_DICT_KEYS // 5)
            evict_keys = list(self._requests.keys())[:evict_count]
            for key in evict_keys:
                del self._requests[key]
            logger.warning(
                f"Rate limiter evicted {evict_count} keys (dict overflow). "
                "Possible IP-flood attack in progress."
            )

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self.enabled:
            return await call_next(request)

        path = request.url.path
        prefix, limit, window = self._find_rule(path)

        if limit == 0:
            return await call_next(request)

        client_ip = self._get_client_ip(request)
        key = f"{client_ip}:{prefix}"

        now = time.time()
        cutoff = now - window

        timestamps = self._requests[key]
        timestamps[:] = [t for t in timestamps if t > cutoff]

        if len(timestamps) >= limit:
            retry_after = int(timestamps[0] + window - now) + 1
            logger.warning(
                f"Rate limited: {client_ip} on {prefix} "
                f"({len(timestamps)}/{limit} in {window}s)"
            )
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Quá nhiều request. Vui lòng thử lại sau {retry_after}s.",
                    "retry_after": retry_after,
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(timestamps[0] + window)),
                },
            )

        timestamps.append(now)
        remaining = limit - len(timestamps)

        self._cleanup_old_entries()

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)

        return response
