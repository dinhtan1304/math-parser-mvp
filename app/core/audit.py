"""
Security audit logging.

Logs security-relevant events (login, logout, register, password reset,
file upload, admin actions) to a dedicated logger for monitoring and
incident response.

Format: {timestamp, user_id, action, ip, details}
"""

import logging
import time
import json
from typing import Any, Optional

# Dedicated audit logger — can be routed to a separate file/service via logging config
_audit_logger = logging.getLogger("audit.security")


def audit_log(
    action: str,
    *,
    user_id: Optional[int] = None,
    ip: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> None:
    """Log a security-relevant event.

    Args:
        action: Event type (login_success, login_failed, logout, register,
                password_reset_request, password_reset_complete, file_upload, admin_action)
        user_id: The user performing the action (None for failed logins)
        ip: Client IP address
        details: Additional context (e.g., target user for admin actions)
    """
    record = {
        "timestamp": time.time(),
        "action": action,
        "user_id": user_id,
        "ip": ip,
    }
    if details:
        record["details"] = details

    _audit_logger.info(json.dumps(record, ensure_ascii=False))
