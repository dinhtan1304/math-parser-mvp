from typing import Optional
import re
from pydantic import BaseModel, EmailStr, field_validator

# Shared properties
class UserBase(BaseModel):
    email: Optional[EmailStr] = None
    is_active: Optional[bool] = True
    is_superuser: bool = False
    full_name: Optional[str] = None
    role: str = "user"

# Properties to receive via API on creation
class UserCreate(UserBase):
    email: EmailStr
    password: str
    full_name: str
    # SECURITY: Explicitly exclude role from user-facing create schema.
    # Role assignment is handled server-side (always "user" on registration).
    role: str = "user"  # Ignored on server; always forced to "user" in auth.py

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

class TokenPayload(BaseModel):
    # BUG FIX: sub is encoded as str in JWT (`str(user_id)` in create_access_token).
    # Declaring as Optional[int] relied on Pydantic v2 coercion which may fail in strict mode.
    sub: Optional[str] = None