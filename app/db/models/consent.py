"""Nhật ký đồng ý và phiên bản chính sách (Luật BVDLCN 91/2025, NĐ 356/2025).

Hai bảng:
  - PolicyVersion : phiên bản hiện hành của từng loại chính sách. Khi tăng phiên
                    bản, người dùng cần đồng ý lại.
  - ConsentLog    : mỗi lần người dùng đồng ý / rút lại đồng ý = 1 dòng bất biến,
                    kèm phiên bản chính sách, IP và user-agent để chứng minh tuân thủ.

Lưu ý pháp lý: IP và user-agent bản thân là dữ liệu cá nhân → việc thu thập chúng
cho mục đích chứng minh tuân thủ PHẢI được khai trong chính sách bảo mật
(content/legal/chinh-sach-bao-mat.md, mục 2.3).
"""
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Index

from app.db.base_class import Base


def _now():
    return datetime.now(timezone.utc)


# Loại đồng ý
TERMS = "TERMS"                      # Điều khoản sử dụng (bắt buộc)
PRIVACY = "PRIVACY"                  # Chính sách bảo mật (bắt buộc)
MARKETING = "MARKETING"              # Nhận email/thông báo tiếp thị (tùy chọn)
CONTENT_LICENSE = "CONTENT_LICENSE"  # Cam kết bản quyền khi chia sẻ lên cộng đồng

CONSENT_TYPES = (TERMS, PRIVACY, MARKETING, CONTENT_LICENSE)
# Loại bắt buộc phải có đồng ý hiện hành mới được dùng dịch vụ.
REQUIRED_CONSENT_TYPES = (TERMS, PRIVACY)

# Hành động
GRANTED = "GRANTED"
WITHDRAWN = "WITHDRAWN"


class PolicyVersion(Base):
    """Phiên bản chính sách đang có hiệu lực cho từng loại."""
    __tablename__ = "policy_version"

    id             = Column(Integer, primary_key=True, index=True)
    policy_type    = Column(String(30), nullable=False)   # TERMS | PRIVACY | ...
    version        = Column(String(20), nullable=False)   # "1.0"
    effective_date = Column(DateTime(timezone=True), nullable=False, default=_now)
    # Tóm tắt thay đổi, hiển thị trong modal khi yêu cầu đồng ý lại.
    summary        = Column(Text, nullable=True)
    created_at     = Column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_policy_version_type_date", "policy_type", "effective_date"),
    )


class ConsentLog(Base):
    """Bản ghi bất biến mỗi lần đồng ý / rút lại đồng ý."""
    __tablename__ = "consent_log"

    id            = Column(Integer, primary_key=True, index=True)
    # NULL cho khách (chưa có tài khoản). Khi xóa tài khoản, user_id được gỡ về
    # NULL để giữ bản ghi ở dạng ẩn danh làm bằng chứng tuân thủ.
    user_id       = Column(Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True)

    consent_type  = Column(String(30), nullable=False)
    policy_version = Column(String(20), nullable=False)
    action        = Column(String(15), nullable=False)   # GRANTED | WITHDRAWN

    # Bằng chứng tuân thủ — chỉ dùng cho mục đích này, không dùng để theo dõi.
    ip_address    = Column(String(45), nullable=True)    # đủ cho IPv6
    user_agent    = Column(String(400), nullable=True)

    created_at    = Column(DateTime(timezone=True), default=_now, index=True)

    __table_args__ = (
        Index("ix_consent_log_user_type", "user_id", "consent_type", "created_at"),
    )
