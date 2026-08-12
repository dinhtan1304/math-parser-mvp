"""Gửi email qua SMTP.

Cố ý tối giản: chỉ đủ cho email giao dịch (xác nhận xóa tài khoản, đặt lại mật
khẩu). Khi ``EMAIL_ENABLED=False`` hoặc thiếu cấu hình SMTP, hàm gửi trả về False
thay vì ném lỗi — nơi gọi phải có đường đi thay thế, KHÔNG được để chức năng
pháp định phụ thuộc vào email.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    """True khi đủ điều kiện gửi email thật."""
    return bool(settings.EMAIL_ENABLED and settings.SMTP_HOST and settings.SMTP_FROM)


def _send_sync(to: str, subject: str, body: str) -> bool:
    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as smtp:
        smtp.starttls()
        if settings.SMTP_USER and settings.SMTP_PASSWORD:
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        smtp.send_message(msg)
    return True


async def send_email(to: str, subject: str, body: str) -> bool:
    """Gửi email. Trả về False (không ném lỗi) khi chưa cấu hình hoặc gửi hỏng."""
    if not is_configured():
        logger.info("Email chưa bật — bỏ qua gửi tới %s (%s)", to, subject)
        return False
    try:
        return await asyncio.to_thread(_send_sync, to, subject, body)
    except Exception as e:
        logger.warning("Gửi email tới %s thất bại: %s", to, e)
        return False


async def send_delete_confirmation(to: str, cancel_token: str) -> bool:
    """Báo đã nhận yêu cầu xóa tài khoản + gửi mã hủy."""
    days = settings.ACCOUNT_DELETE_GRACE_DAYS
    body = (
        "Chúng tôi đã nhận được yêu cầu xóa tài khoản Edu Smart App của bạn.\n\n"
        f"Tài khoản sẽ bị xóa vĩnh viễn sau {days} ngày.\n\n"
        "Nếu bạn KHÔNG yêu cầu điều này, hãy dùng mã hủy dưới đây để khôi phục "
        "tài khoản ngay:\n\n"
        f"    {cancel_token}\n\n"
        "Nhập mã này cùng địa chỉ email của bạn tại trang hủy yêu cầu xóa.\n\n"
        "— Edu Smart App"
    )
    return await send_email(to, "Xác nhận yêu cầu xóa tài khoản", body)


async def send_reset_email(to: str, reset_url: str) -> bool:
    """Gửi link đặt lại mật khẩu."""
    body = (
        "Bạn (hoặc ai đó) đã yêu cầu đặt lại mật khẩu Edu Smart App.\n\n"
        f"Mở liên kết sau để đặt mật khẩu mới:\n\n    {reset_url}\n\n"
        "Liên kết có hiệu lực trong 1 giờ. Nếu bạn không yêu cầu, hãy bỏ qua email này.\n\n"
        "— Edu Smart App"
    )
    return await send_email(to, "Đặt lại mật khẩu Edu Smart App", body)
