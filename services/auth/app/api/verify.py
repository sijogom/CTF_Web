import logging
import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.email.sender import send_email
from app.email.templates import email_verification_template
from app.models.user import User
from app.schemas.auth import EmailVerificationCodeRequest
from app.utils.audit import record_audit
from app.utils.email_verification import (
    can_resend_verification_code,
    clear_verification_artifacts,
    issue_verification_code,
    utc_now,
    verify_verification_code,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["이메일 인증"])


@router.post("/send-verification")
async def send_verification_email(
    request: Request,
    email: EmailStr = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """인증 수단을 재발송한다.

    신규 코드 인증 계정과 기존 링크 인증 계정을 모두 지원한다. 존재 여부와
    재발송 제한 상태를 동일한 응답으로 처리하고, 코드·토큰은 응답·감사 로그에 넣지 않는다.
    """
    result = await db.execute(select(User).where(User.email == email).with_for_update())
    user = result.scalar_one_or_none()
    generic_response = {"message": "인증 메일이 발송되었습니다."}

    if not user or user.email_verified:
        return generic_response

    now = utc_now()
    html: str
    subject: str
    if user.email_verify_code_required:
        if not can_resend_verification_code(user, now=now):
            return generic_response
        code = issue_verification_code(user, now=now)
        subject = "[CTF Platform] 이메일 인증 코드"
        html = email_verification_template(user.username, verification_code=code)
    else:
        # 코드 도입 전 발송된 링크 인증 계정의 호환 경로
        token = secrets.token_urlsafe(48)
        user.email_verify_token = token
        user.email_verify_expires = now + timedelta(hours=settings.email_verify_expire_hours)
        subject = "[CTF Platform] 이메일 인증"
        verify_url = f"{settings.frontend_url}/verify-email?token={token}"
        html = email_verification_template(user.username, verify_url)

    try:
        await record_audit(
            db,
            actor_id=str(user.id),
            action="auth.verification_resent",
            target_type="user",
            target_id=str(user.id),
            details={"method": "code" if user.email_verify_code_required else "legacy_link"},
            request=request,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("인증 재발송 상태 또는 감사 기록 저장 실패")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="인증 메일을 준비하지 못했습니다.")

    try:
        await send_email(user.email, subject, html)
    except Exception:
        logger.warning("인증 재발송 실패 (user_id=%s)", user.id, exc_info=True)
        try:
            await record_audit(
                db,
                actor_id=str(user.id),
                action="auth.verification_delivery_failed",
                target_type="user",
                target_id=str(user.id),
                details={"channel": "email", "resend": True},
                request=request,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("인증 재발송 실패 감사 기록 저장 실패")
        # 계정 존재 여부를 노출하지 않도록 응답은 동일하게 유지한다.
    return generic_response


@router.post("/verify-email/code")
async def verify_email_code(
    body: EmailVerificationCodeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """신규 가입자의 일회용 이메일 인증 코드를 검증한다."""
    result = await db.execute(select(User).where(User.email == body.email).with_for_update())
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="인증 코드를 확인해주세요.")
    if user.email_verified:
        return {"message": "이미 인증된 이메일입니다.", "already_verified": True}

    verification_state = verify_verification_code(user, body.code)
    if verification_state == "success":
        clear_verification_artifacts(user)
        try:
            await record_audit(
                db,
                actor_id=str(user.id),
                action="auth.email_verified",
                target_type="user",
                target_id=str(user.id),
                details={"method": "code"},
                request=request,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("이메일 코드 인증 저장 또는 감사 기록 실패")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="이메일 인증을 완료하지 못했습니다.")
        return {"message": "이메일 인증이 완료되었습니다."}

    try:
        await record_audit(
            db,
            actor_id=str(user.id),
            action="auth.email_verification_failed",
            target_type="user",
            target_id=str(user.id),
            details={"reason": verification_state},
            request=request,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("이메일 코드 인증 실패 감사 기록 저장 실패")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="인증 상태를 저장하지 못했습니다.")

    if verification_state == "expired":
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="인증 코드가 만료되었습니다. 새 코드를 요청해주세요.")
    if verification_state == "locked":
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="인증 코드 시도 횟수를 초과했습니다. 새 코드를 요청해주세요.")
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="인증 코드를 확인해주세요.")


@router.get("/verify-email")
async def verify_email(
    request: Request,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """코드 도입 이전에 발송된 링크 인증을 전환 기간 동안 처리한다."""
    result = await db.execute(select(User).where(User.email_verify_token == token).with_for_update())
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="유효하지 않은 인증 토큰입니다.",
        )

    if user.email_verified:
        return {"message": "이미 인증된 이메일입니다."}

    # 신규 코드 인증 계정은 링크만으로 인증되지 않는다.
    if user.email_verify_code_required:
        return {
            "message": "이메일로 받은 인증 코드를 입력해주세요.",
            "code_required": True,
            "email": user.email,
        }

    if user.email_verify_expires and user.email_verify_expires < utc_now():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="인증 토큰이 만료되었습니다. 인증 메일을 다시 요청해주세요.",
        )

    clear_verification_artifacts(user)
    try:
        await record_audit(
            db,
            actor_id=str(user.id),
            action="auth.email_verified",
            target_type="user",
            target_id=str(user.id),
            details={"method": "legacy_link"},
            request=request,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("기존 링크 이메일 인증 저장 또는 감사 기록 실패")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="이메일 인증을 완료하지 못했습니다.")

    return {"message": "이메일 인증이 완료되었습니다."}


@router.get("/email-unsubscribe")
async def email_unsubscribe_page(
    token: str = Query(...),
):
    """이메일 수신 거부 확인 페이지 — GET은 토큰 유효성만 확인 (상태 변경 없음)."""
    from app.redis import redis_client

    raw_user_id = await redis_client.get(f"email_unsub:{token}")
    if not raw_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="유효하지 않거나 만료된 링크입니다.",
        )
    return {"message": "수신 거부를 확인하려면 POST 요청을 보내세요.", "token_valid": True}


@router.post("/email-unsubscribe")
async def email_unsubscribe(
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """이메일 수신 거부 처리 — POST로만 상태 변경 (프리페치/크롤러 방지)."""
    from app.redis import redis_client

    raw_user_id = await redis_client.get(f"email_unsub:{token}")
    if not raw_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="유효하지 않거나 만료된 링크입니다.",
        )

    try:
        # Redis 클라이언트에 따라 bytes 반환 가능 — 방어 처리
        if isinstance(raw_user_id, bytes):
            raw_user_id = raw_user_id.decode("utf-8")
        parsed_user_id = uuid.UUID(raw_user_id)
    except (ValueError, AttributeError, TypeError, UnicodeDecodeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="유효하지 않은 링크입니다.",
        )

    result = await db.execute(select(User).where(User.id == parsed_user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="유효하지 않은 링크입니다.",
        )

    user.email_notifications = False
    await db.commit()
    await redis_client.delete(f"email_unsub:{token}")

    return {"message": "이메일 수신이 거부되었습니다. 프로필에서 다시 활성화할 수 있습니다."}
