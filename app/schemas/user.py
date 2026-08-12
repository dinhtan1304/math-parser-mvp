from typing import Optional, Literal
import re
from pydantic import BaseModel, EmailStr, field_validator

# Shared properties
class UserBase(BaseModel):
    email: Optional[EmailStr] = None
    is_active: Optional[bool] = True
    # is_superuser đã bỏ khỏi schema (2026-07-10): nguồn sự thật duy nhất là
    # role == "admin" (deps.get_current_active_superuser). Cột DB giữ nguyên
    # để không phải migrate, nhưng không expose/không đọc.
    full_name: Optional[str] = None
    role: str = "teacher"

# Properties to receive via API on creation
class UserCreate(UserBase):
    email: EmailStr
    password: str
    full_name: str
    # Teacher-only pivot: self-registration always creates a teacher account.
    # Admin cannot self-register.
    role: Literal["teacher"] = "teacher"

    # Đồng ý Điều khoản + Chính sách bảo mật — BẮT BUỘC (Luật BVDLCN 91/2025).
    # KHÔNG đặt giá trị mặc định: thiếu trường này thì request bị từ chối. Nếu để
    # mặc định False, Pydantic sẽ bỏ qua validator và cổng đồng ý mất tác dụng.
    accept_terms: bool
    # Đồng ý nhận email/thông báo tiếp thị — TÙY CHỌN, mặc định không đồng ý.
    accept_marketing: bool = False

    @field_validator("accept_terms")
    @classmethod
    def must_accept_terms(cls, v: bool) -> bool:
        if not v:
            raise ValueError(
                "Bạn cần đồng ý với Điều khoản sử dụng và Chính sách bảo mật để tạo tài khoản"
            )
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Mật khẩu phải có ít nhất 8 ký tự")
        if not re.search(r"[A-Za-z]", v):
            raise ValueError("Mật khẩu phải có ít nhất 1 chữ cái")
        if not re.search(r"\d", v):
            raise ValueError("Mật khẩu phải có ít nhất 1 chữ số")
        return v

    @field_validator("full_name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if v and len(v.strip()) < 2:
            raise ValueError("Tên phải có ít nhất 2 ký tự")
        return v.strip()

# Properties to receive via API on update
class UserUpdate(UserBase):
    password: Optional[str] = None

# Properties shared by models stored in DB
class UserInDBBase(UserBase):
    id: int
    
    class Config:
        from_attributes = True

# Properties to return to client
class User(UserInDBBase):
    pass

# Properties stored in DB
class UserInDB(UserInDBBase):
    hashed_password: str

# Token schemas
class Token(BaseModel):
    access_token: str
    token_type: str
    # None cho client cũ; login/refresh luôn trả về giá trị mới (rotate).
    refresh_token: Optional[str] = None

class TokenPayload(BaseModel):
    # BUG FIX: sub is encoded as str in JWT (`str(user_id)` in create_access_token).
    # Declaring as Optional[int] relied on Pydantic v2 coercion which may fail in strict mode.
    sub: Optional[str] = None
    # type: "access" | "refresh"; token cũ (trước 2026-07-10) không có → coi như access.
    type: Optional[str] = None
    jti: Optional[str] = None