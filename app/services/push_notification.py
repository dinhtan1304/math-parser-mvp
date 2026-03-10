"""
Expo Push Notification service.

Sends push notifications via Expo's push API:
POST https://exp.host/--/api/v2/push/send

Tokens are stored in DeviceToken table per user.
"""

import logging
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_EXPO_TIMEOUT = 10  # seconds


async def get_user_tokens(db: AsyncSession, user_id: int) -> list[str]:
    """Fetch all expo push tokens for a user."""
    from app.db.models.notification import DeviceToken
    result = await db.execute(
        select(DeviceToken.expo_push_token).where(DeviceToken.user_id == user_id)
    )
    return list(result.scalars().all())


async def send_push(
    tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> None:
    """Send push notification to a list of Expo tokens.

    Fires and forgets — errors are logged but not raised.
    Expo handles batching internally (up to 100 per request).
    """
    if not tokens:
        return

    messages = [
        {
            "to": token,
            "title": title,
            "body": body,
            "data": data or {},
            "sound": "default",
        }
        for token in tokens
        if token.startswith("ExponentPushToken[") or token.startswith("ExpoPushToken[")
    ]
    if not messages:
        logger.debug("No valid Expo push tokens to send")
        return

    try:
        async with httpx.AsyncClient(timeout=_EXPO_TIMEOUT) as client:
            resp = await client.post(
                EXPO_PUSH_URL,
                json=messages,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            if resp.status_code != 200:
                logger.warning(f"Expo push API returned {resp.status_code}: {resp.text[:200]}")
            else:
                data_resp = resp.json().get("data", [])
                errors = [d for d in data_resp if d.get("status") == "error"]
                if errors:
                    logger.warning(f"Expo push errors: {errors[:3]}")
                else:
                    logger.info(f"Push sent to {len(messages)} device(s)")
    except Exception as e:
        logger.warning(f"Push notification failed: {e}")


async def send_push_to_user(
    db: AsyncSession,
    user_id: int,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> None:
    """Convenience: fetch tokens for user_id and send push."""
    tokens = await get_user_tokens(db, user_id)
    if tokens:
        await send_push(tokens, title, body, data)
