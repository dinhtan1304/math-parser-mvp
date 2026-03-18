"""
Request ID middleware.

Generates a unique UUID for each request, sets it in the response header
(X-Request-ID), and makes it available for logging context.
"""

import uuid
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

logger = logging.getLogger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        # Store in request state for downstream access
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
