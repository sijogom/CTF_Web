"""신규 가입 이메일 인증 코드의 발급·검증 보조 함수.

코드 원문은 DB·감사 로그에 저장하지 않는다. 사용자 ID를 포함한 HMAC으로 저장해
다른 계정의 해시를 재사용할 수 없게 하고, 기존 링크 인증 계정과는 별도 필드로
전환 호환성을 유지한다.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models.user import User


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def generate_verification_code() -> str:
    """추측 방지를 위해 난수 기반 6자리 숫자 코드를 만든다."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_verification_code(user_id: object, code: str) -> str:
    message = f"email-verification-code:v1:{user_id}:{code}".encode("utf-8")
    key = settings.jwt_secret_key.encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def issue_verification_code(user: User, *, now: datetime | None = None) -> str:
    """새 코드를 발급하고 사용자 레코드의 코드 관련 상태를 초기화한다."""
    issued_at = now or utc_now()
    code = generate_verification_code()
    user.email_verify_code_required = True
    # 코드 인증으로 전환한 계정은 남아 있던 링크 토큰만으로 인증되지 않게 한다.
    user.email_verify_token = None
    user.email_verify_expires = None
    user.email_verify_code_hash = hash_verification_code(user.id, code)
    user.email_verify_code_expires = issued_at + timedelta(
        minutes=settings.email_verify_code_expire_minutes,
    )
    user.email_verify_code_sent_at = issued_at
    user.email_verify_code_attempts = 0
    return code


def can_resend_verification_code(user: User, *, now: datetime | None = None) -> bool:
    if not user.email_verify_code_sent_at:
        return True
    current = now or utc_now()
    cooldown = timedelta(minutes=settings.email_verify_resend_cooldown_minutes)
    return current >= user.email_verify_code_sent_at + cooldown


def verify_verification_code(
    user: User,
    code: str,
    *,
    now: datetime | None = None,
) -> str:
    """코드를 확인하고 ``success``/``expired``/``locked``/``invalid``를 반환한다.

    호출자는 반환 뒤 변경된 시도 횟수를 반드시 같은 DB 트랜잭션에서 커밋해야 한다.
    """
    current = now or utc_now()
    if not user.email_verify_code_required or not user.email_verify_code_hash:
        return "invalid"
    if user.email_verify_code_expires is None or user.email_verify_code_expires <= current:
        return "expired"
    if user.email_verify_code_attempts >= settings.email_verify_code_max_attempts:
        return "locked"

    expected = hash_verification_code(user.id, code)
    if not hmac.compare_digest(user.email_verify_code_hash, expected):
        user.email_verify_code_attempts += 1
        if user.email_verify_code_attempts >= settings.email_verify_code_max_attempts:
            return "locked"
        return "invalid"
    return "success"


def clear_verification_artifacts(user: User) -> None:
    """이메일 인증 성공 뒤 코드·링크 자격 증명을 모두 한 번에 폐기한다."""
    user.email_verified = True
    user.email_verify_token = None
    user.email_verify_expires = None
    user.email_verify_code_required = False
    user.email_verify_code_hash = None
    user.email_verify_code_expires = None
    user.email_verify_code_sent_at = None
    user.email_verify_code_attempts = 0
