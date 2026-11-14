import uuid
from datetime import datetime, timedelta, timezone
import jwt
from app.config import settings


def _get_decode_key() -> str:
    """RS256이면 public key, HS256이면 secret key 반환"""
    if settings.jwt_algorithm == "RS256" and settings.jwt_public_key:
        return settings.jwt_public_key
    return settings.jwt_secret_key


def create_access_token(user_id: str, role: str, username: str = "") -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": user_id,
        "role": role,
        "username": username,
        "exp": expire,
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: str) -> str:
    """Refresh Token 생성 — jti(JWT ID)로 1회용 사용 보장"""
    jti = str(uuid.uuid4())
    expire = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    payload = {
        "sub": user_id,
        "jti": jti,          # Redis에 저장하여 1회용 사용 보장
        "exp": expire,
        "type": "refresh",
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_ban_appeal_token(user_id: str, ban_id: str) -> str:
    """밴 이의제기 전용의 짧은 수명 JWT를 만든다."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=30)
    payload = {
        "sub": user_id,
        "ban_id": ban_id,
        "exp": expire,
        "type": "ban_appeal",
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict | None:
    """JWT 디코드 — RS256이면 public key, HS256이면 secret key로 검증"""
    try:
        return jwt.decode(token, _get_decode_key(), algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
