import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.config import settings
from app.database import get_db
from app.models.user import User, PasswordReset
from app.security import pwd_context
from app.email.sender import mask_email, send_email
from app.email.templates import password_reset_template
from app.utils.audit import record_audit

logger = logging.getLogger(__name__)
router = APIRouter(tags=["비밀번호"])


async def _record_password_audit(
    db: AsyncSession,
    request: Request,
    *,
    actor_id: str | None,
    action: str,
    outcome: str,
) -> None:
    """비밀번호 흐름 감사 기록을 남기되 토큰·비밀번호·이메일은 저장하지 않는다."""
    try:
        await record_audit(
            db,
            actor_id=actor_id,
            action=action,
            target_type="user" if actor_id else None,
            target_id=actor_id,
            details={"outcome": outcome},
            request=request,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("비밀번호 흐름 감사 기록 저장 실패", exc_info=True)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        """공통 비밀번호 복잡도 규칙 적용."""
        from app.utils.password import validate_password_complexity
        return validate_password_complexity(v)


@router.post("/forgot-password")
async def forgot_password(
    req: ForgotPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """비밀번호 재설정 메일 발송"""
    result = await db.execute(select(User).where(User.email == req.email))
    user = result.scalar_one_or_none()

    # 보안: 존재 여부를 노출하지 않음 (비활성 계정도 동일 처리)
    if not user or not user.is_active:
        await _record_password_audit(
            db,
            request,
            actor_id=None,
            action="auth.password_reset_requested",
            outcome="generic_response",
        )
        return {"message": "해당 이메일로 비밀번호 재설정 링크를 발송했습니다."}

    # 토큰 생성
    token = secrets.token_urlsafe(48)
    reset = PasswordReset(
        user_id=user.id,
        token=token,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.password_reset_expire_hours),
    )
    db.add(reset)
    await _record_password_audit(
        db,
        request,
        actor_id=str(user.id),
        action="auth.password_reset_requested",
        outcome="accepted",
    )

    # 이메일 발송
    reset_url = f"{settings.frontend_url}/reset-password/{token}"
    html = password_reset_template(user.username, reset_url)

    try:
        await send_email(user.email, "[CTF Platform] 비밀번호 재설정", html)
    except Exception:
        logger.warning(
            "비밀번호 재설정 메일 발송 실패: %s",
            mask_email(user.email), exc_info=True,
        )

    return {"message": "해당 이메일로 비밀번호 재설정 링크를 발송했습니다."}


@router.post("/reset-password")
async def reset_password(
    req: ResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """비밀번호 재설정"""
    # FOR UPDATE: 동시 요청 시 토큰 이중 사용 방지 (TOCTOU race condition)
    result = await db.execute(
        select(PasswordReset).where(
            PasswordReset.token == req.token,
            PasswordReset.used == False,  # noqa: E712
        ).with_for_update()
    )
    reset = result.scalar_one_or_none()

    if not reset:
        await _record_password_audit(
            db,
            request,
            actor_id=None,
            action="auth.password_reset_failed",
            outcome="invalid_or_used_token",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="유효하지 않은 재설정 토큰입니다.",
        )

    if reset.expires_at < datetime.now(timezone.utc):
        await _record_password_audit(
            db,
            request,
            actor_id=None,
            action="auth.password_reset_failed",
            outcome="expired_token",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="재설정 토큰이 만료되었습니다.",
        )

    # 비밀번호 변경
    user_result = await db.execute(select(User).where(User.id == reset.user_id))
    user = user_result.scalar_one_or_none()

    if not user:
        await _record_password_audit(
            db,
            request,
            actor_id=None,
            action="auth.password_reset_failed",
            outcome="user_not_found",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="유저를 찾을 수 없습니다.",
        )

    user.password_hash = pwd_context.hash(req.new_password)
    reset.used = True
    await _record_password_audit(
        db,
        request,
        actor_id=str(user.id),
        action="auth.password_reset_completed",
        outcome="success",
    )

    return {"message": "비밀번호가 성공적으로 변경되었습니다."}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        """공통 비밀번호 복잡도 규칙 적용."""
        from app.utils.password import validate_password_complexity
        return validate_password_complexity(v)


@router.patch("/password")
async def change_password(
    req: ChangePasswordRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """로그인된 사용자의 비밀번호 변경"""
    # OAuth 전용 계정은 password_hash가 None → verify() TypeError 방지
    if not user.password_hash:
        await _record_password_audit(
            db,
            request,
            actor_id=str(user.id),
            action="auth.password_change_failed",
            outcome="social_account",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="소셜 로그인 계정은 비밀번호를 변경할 수 없습니다.",
        )
    if not pwd_context.verify(req.current_password, user.password_hash):
        await _record_password_audit(
            db,
            request,
            actor_id=str(user.id),
            action="auth.password_change_failed",
            outcome="current_password_mismatch",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="현재 비밀번호가 올바르지 않습니다.",
        )
    user.password_hash = pwd_context.hash(req.new_password)
    await _record_password_audit(
        db,
        request,
        actor_id=str(user.id),
        action="auth.password_change_completed",
        outcome="success",
    )
    return {"message": "비밀번호가 변경되었습니다."}
