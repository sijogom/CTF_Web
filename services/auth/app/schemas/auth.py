import re
import unicodedata
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.utils.password import validate_password_complexity


ALLOWED_EMAIL_DOMAINS = ["cju.ac.kr"]


class RegisterRequest(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def email_domain_check(cls, v: str) -> str:
        """허용된 이메일 도메인만 가입 가능"""
        domain = v.split("@")[-1].lower()
        if domain not in ALLOWED_EMAIL_DOMAINS:
            allowed = ", ".join(ALLOWED_EMAIL_DOMAINS)
            raise ValueError(f"허용된 이메일 도메인: {allowed}")
        return v

    @field_validator("username")
    @classmethod
    def username_pattern(cls, v: str) -> str:
        """사용자명 정책: NFC, 공백 없음, CJK·영문·숫자·_·-만 허용."""
        normalized = unicodedata.normalize("NFC", v)
        if normalized != normalized.strip():
            raise ValueError("사용자명 앞뒤 공백은 사용할 수 없습니다.")
        if not re.fullmatch(r"[A-Za-z0-9_가-힣\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF-]+", normalized):
            raise ValueError("사용자명은 한글, 일본어, 중국어, 영문, 숫자, 밑줄(_), 하이픈(-)만 사용할 수 있습니다.")
        return normalized

    @field_validator("password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        """최소 8자, 대문자 1개, 소문자 1개, 숫자 1개, 특수문자 1개 필수"""
        return validate_password_complexity(v)


class RegisterResponse(BaseModel):
    id: str
    email: str
    username: str
    message: str = "회원가입이 완료되었습니다."
    auto_verified: bool = False
    verification_required: bool = True
    delivery_status: str = "sent"


class EmailVerificationCodeRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6)

    @field_validator("code")
    @classmethod
    def verification_code_format(cls, v: str) -> str:
        if not v.isascii() or not v.isdigit():
            raise ValueError("인증 코드는 숫자 6자리여야 합니다.")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    token_type: str = "bearer"
    expires_in: int


class UserProfile(BaseModel):
    id: str
    email: str
    username: str
    role: str
    level: int
    xp: int
    points: int = 0
    streak_days: int
    avatar_url: str | None = None
    bio: str | None = None
    email_verified: bool = False
    email_notifications: bool = True
    totp_enabled: bool = False
    discord_id: str | None = None
    discord_username: str | None = None
    baekjoon_handle: str | None = None
    baekjoon_tier: int | None = None
    tier_name: str | None = None
    tier_image: str | None = None
