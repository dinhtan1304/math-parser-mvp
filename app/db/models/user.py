from sqlalchemy import Boolean, Column, Integer, String, DateTime
from app.db.base_class import Base


class User(Base):
    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String, index=True, nullable=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    role = Column(String, default="teacher")
    reset_token = Column(String, nullable=True)
    reset_token_expires = Column(DateTime(timezone=True), nullable=True)

    # Soft-delete cho quyền xóa dữ liệu (Luật BVDLCN 91/2025).
    # NULL = tài khoản đang hoạt động. Khác NULL = đang trong thời gian chờ xóa
    # (grace period, người dùng còn hủy được), sau đó job retention xóa vĩnh viễn.
    #
    # QUAN TRỌNG: mọi truy vấn xác thực (login, refresh, forgot-password,
    # get_current_user) PHẢI loại user có deleted_at IS NOT NULL — xem
    # app/api/auth.py và app/api/deps.py.
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # Mã hủy yêu cầu xóa (đã băm). Vì tài khoản chờ xóa KHÔNG đăng nhập được,
    # đây là đường duy nhất để chủ tài khoản tự khôi phục trong thời gian grace.
    delete_cancel_token = Column(String, nullable=True)
