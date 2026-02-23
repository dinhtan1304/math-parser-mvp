"""
In-memory rate limiter middleware (Sprint 2, Task 13).

Lightweight, zero-dependency approach for MVP.
Uses sliding window per (user_ip, path_prefix).

Rate limits:
  - /api/v1/auth/login:     5/min   (brute-force protection)
  - /api/v1/auth/register:  3/min   (spam protection)
  - /api/v1/parser/parse:   10/min  (costs Gemini API $$$)
  - /api/v1/generate:       10/min  (costs Gemini API $$$)
  - Default API:            60/min

For production at scale, replace with Redis-backed limiter or API gateway.
"""

import time
import logging
from collections import defaultdict
from typing import Dict, List, Tuple

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


# ── Rate limit rules: (path_prefix, max_requests, window_seconds) ──
RATE_RULES: List[Tuple[str, int, int]] = [
    ("/api/v1/auth/login",    5,  60),    # 5 req/min
    ("/api/v1/auth/register", 3,  60),    # 3 req/min
    ("/api/v1/parser/parse",  10, 60),    # 10 req/min
    ("/api/v1/generate",      10, 60),    # 10 req/min
]

DEFAULT_LIMIT = 60        # 60 req/min for all other API endpoints
DEFAULT_WINDOW = 60       # 60 seconds


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window in-memory rate limiter."""

    def __init__(self, app, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled
        # Key: (client_ip, rule_prefix) → List of timestamps
        self._requests: Dict[str, List[float]] = defaultdict(list)
        self._last_cleanup = time.time()
        self._cleanup_interval = 300  # Cleanup every 5 min

    def _get_client_ip(self, request: Request) -> str:
        """Get real client IP (handles proxies)."""
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _find_rule(self, path: str) -> Tuple[str, int, int]:
        """Find matching rate limit rule for path."""
        for prefix, limit, window in RATE_RULES:
            if path.startswith(prefix):
                return prefix, limit, window
        # Default for any /api/ path
        if path.startswith("/api/"):
            return "/api/", DEFAULT_LIMIT, DEFAULT_WINDOW
        # No limit for non-API paths (HTML, static)
        return "", 0, 0

    def _cleanup_old_entries(self):
        """Periodically remove expired entries to prevent memory leak."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return

        self._last_cleanup = now
        cutoff = now - 300  # Remove entries older than 5 min
        keys_to_delete = []

        for key, timestamps in self._requests.items():
            # Filter in-place
            self._requests[key] = [t for t in timestamps if t > cutoff]
            if not self._requests[key]:
                keys_to_delete.append(key)

        for key in keys_to_delete:
            del self._requests[key]

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self.enabled:
            return await call_next(request)

        path = request.url.path
        prefix, limit, window = self._find_rule(path)

        # No rate limit for this path
        if limit == 0:
            return await call_next(request)

        # Build key
        client_ip = self._get_client_ip(request)
        key = f"{client_ip}:{prefix}"

        now = time.time()
        cutoff = now - window

        # Sliding window: keep only timestamps within window
        timestamps = self._requests[key]
        timestamps[:] = [t for t in timestamps if t > cutoff]

        # Check limit
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

        # Allow request
        timestamps.append(now)
        remaining = limit - len(timestamps)

        # Periodic cleanup
        self._cleanup_old_entries()

        # Process request and add rate limit headers
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)

        return response