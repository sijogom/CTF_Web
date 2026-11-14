"""관리자 이메일 발송 API — 모듈 분리."""

import json
import logging
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import EmailLog, User
from app.api.deps import require_role
from app.email.sender import mask_email, send_email
from app.email.templates import admin_email_template, admin_email_preview_template, test_email_template
from app.redis import redis_client
from app.utils.audit import record_audit
from app.utils.admin_settings import (
    ADMIN_SETTINGS_UNAVAILABLE_DETAIL,
    SMTP_SETTINGS_INVALID_DETAIL,
    AdminSettingsStorageError,
    InvalidSmtpSettings,
    load_admin_settings,
    prepare_smtp_settings_update,
    refresh_admin_settings_cache,
    save_admin_settings,
)
from app.utils.crypto import decrypt_value
from app.utils.safe_html import RichContentError, to_safe_rich_html

logger = logging.getLogger(__name__)
router = APIRouter(tags=["관리자 이메일"])

VALID_ROLES = {"user", "moderator", "admin"}


def _normalize_email_body_or_422(value: str) -> str:
    try:
        return to_safe_rich_html(value, field_name="이메일 본문")
    except RichContentError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error.as_detail(),
        ) from error


class AdminSendEmailRequest(BaseModel):
    target: Literal["user", "role", "all"]
    user_ids: list[str] | None = None
    role: str | None = None
    subject: str
    body: str


@router.post("/email/send")
async def admin_send_email(
    req: AdminSendEmailRequest,
    request: Request,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """관리자 이메일 발송 — 대상별(개별/역할/전체) 이메일 발송."""
    # 대상 사용자 조회
    if req.target == "user":
        if not req.user_ids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="user_ids가 필요합니다.")
        result = await db.execute(
            select(User).where(
                User.id.in_(req.user_ids),
                User.is_active == True,  # noqa: E712
                User.email_verified == True,  # noqa: E712
                User.email_notifications == True,  # noqa: E712
            )
        )
        recipients = result.scalars().all()
    elif req.target == "role":
        if not req.role or req.role not in VALID_ROLES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"유효하지 않은 역할입니다. 허용: {', '.join(VALID_ROLES)}",
            )
        result = await db.execute(
            select(User).where(
                User.role == req.role,
                User.is_active == True,  # noqa: E712
                User.email_verified == True,  # noqa: E712
                User.email_notifications == True,  # noqa: E712
            )
        )
        recipients = result.scalars().all()
    else:  # "all"
        result = await db.execute(
            select(User).where(
                User.is_active == True,  # noqa: E712
                User.email_verified == True,  # noqa: E712
                User.email_notifications == True,  # noqa: E712
            )
        )
        recipients = result.scalars().all()

    if not recipients:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="발송 대상 사용자가 없습니다.")

    MAX_RECIPIENTS = 500
    if len(recipients) > MAX_RECIPIENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"수신자 수가 최대 {MAX_RECIPIENTS}명을 초과합니다. (현재 {len(recipients)}명)",
        )

    # Subject SMTP 헤더 인젝션 방지 + 본문 제한 HTML 정화
    safe_subject = req.subject.replace("\r", "").replace("\n", "")
    safe_body = _normalize_email_body_or_422(req.body)

    # 수신거부 토큰 유효기간
    _UNSUB_TOKEN_TTL = settings.email_unsub_expire_days * 86400

    success_count = 0
    fail_count = 0
    error_details: dict[str, str] = {}
    for user in recipients:
        try:
            # 수신자별 수신거부 토큰 생성
            unsub_token = secrets.token_urlsafe(32)
            await redis_client.set(
                f"email_unsub:{unsub_token}", str(user.id), ex=_UNSUB_TOKEN_TTL,
            )
            unsub_url = f"{settings.frontend_url}/email-unsubscribe?token={unsub_token}"

            # 수신자별 고유 HTML 생성 (수신거부 링크 포함)
            user_html = admin_email_template(safe_body, unsubscribe_url=unsub_url)
            await send_email(user.email, safe_subject, user_html)
            success_count += 1
        except Exception as exc:
            error_type = type(exc).__name__
            logger.warning("이메일 발송 실패 (to=%s, error_type=%s)", mask_email(user.email), error_type)
            fail_count += 1
            error_details[mask_email(user.email)] = error_type

    # EmailLog 저장
    log = EmailLog(
        sender_id=admin.id,
        recipients=json.dumps([mask_email(u.email) for u in recipients]),
        subject=safe_subject,
        target_type=req.target,
        total_count=len(recipients),
        success_count=success_count,
        fail_count=fail_count,
        error_details=json.dumps(error_details) if error_details else None,
    )
    db.add(log)

    await record_audit(
        db,
        actor_id=str(admin.id),
        action="email.send",
        target_type="email",
        details={
            "target": req.target,
            "subject_length": len(safe_subject),
            "recipient_count": len(recipients),
            "success": success_count,
            "failed": fail_count,
        },
        request=request,
    )
    await db.commit()

    return {
        "message": f"이메일 발송 완료: 성공 {success_count}건, 실패 {fail_count}건",
        "success": success_count,
        "failed": fail_count,
    }


class EmailPreviewRequest(BaseModel):
    subject: str
    body: str
    locale: str = "ko"


@router.post("/email/preview")
async def preview_email(
    req: EmailPreviewRequest,
    admin: User = Depends(require_role("admin")),
):
    """이메일 미리보기 — HTML 렌더링 결과 반환."""
    safe_subject = req.subject.replace("\r", "").replace("\n", "")
    safe_body = _normalize_email_body_or_422(req.body)
    html_content = admin_email_preview_template(safe_body, locale=req.locale)
    return {"html": html_content, "subject": safe_subject}


@router.get("/email/logs")
async def get_email_logs(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """발송 이력 조회 — admin만 가능. 최신순 페이지네이션."""
    offset = (page - 1) * size
    count_result = await db.execute(select(func.count(EmailLog.id)))
    total = count_result.scalar()

    result = await db.execute(
        select(EmailLog).order_by(EmailLog.created_at.desc()).offset(offset).limit(size)
    )
    logs = result.scalars().all()

    return {
        "items": [
            {
                "id": str(log.id),
                "sender_id": str(log.sender_id) if log.sender_id else None,
                "recipients": json.loads(log.recipients),
                "subject": log.subject,
                "target_type": log.target_type,
                "total_count": log.total_count,
                "success_count": log.success_count,
                "fail_count": log.fail_count,
                "error_details": json.loads(log.error_details) if log.error_details else None,
                "created_at": log.created_at.isoformat(),
            }
            for log in logs
        ],
        "total": total,
        "page": page,
        "size": size,
    }


# ──── SMTP 설정 관리 ────


class SmtpSettingsRequest(BaseModel):
    smtp_host: str
    smtp_port: int = Field(ge=1, le=65535)
    smtp_from: str
    smtp_tls: bool = True
    smtp_user: str = ""
    smtp_password: str = ""


@router.get("/email/smtp-settings")
async def get_smtp_settings(
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """PostgreSQL 기준 SMTP 설정을 마스킹하여 조회한다."""
    try:
        data, source = await load_admin_settings(db)
    except AdminSettingsStorageError as exc:
        logger.warning("SMTP 설정 조회 실패: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ADMIN_SETTINGS_UNAVAILABLE_DETAIL,
        ) from exc

    password = data.get("smtp_password", "")
    password = password if isinstance(password, str) else ""
    decrypted = decrypt_value(password) if password else ""
    masked = decrypted[:2] + "***" if len(decrypted) > 2 else ("***" if decrypted else "")
    return {
        "smtp_host": data.get("smtp_host", settings.smtp_host),
        "smtp_port": data.get("smtp_port", settings.smtp_port),
        "smtp_from": data.get("smtp_from", settings.smtp_from),
        "smtp_tls": data.get("smtp_tls", settings.smtp_tls),
        "smtp_user": data.get("smtp_user", settings.smtp_user),
        "smtp_password_masked": masked,
        "source": source,
        "cache_refresh_pending": source.endswith("_cache_unavailable"),
    }


@router.put("/email/smtp-settings")
async def update_smtp_settings(
    body: SmtpSettingsRequest,
    request: Request,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """SMTP 설정을 공통 관리자 설정 저장소와 감사 로그에 함께 저장한다."""
    try:
        updates = prepare_smtp_settings_update(
            body.model_dump(),
            require_connection_fields=True,
        )
    except InvalidSmtpSettings as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=SMTP_SETTINGS_INVALID_DETAIL,
        ) from exc

    try:
        current, _ = await load_admin_settings(db)
        current.update(updates)

        await save_admin_settings(db, current)
        await record_audit(
            db,
            actor_id=str(admin.id),
            action="settings.smtp_update",
            target_type="settings",
            details={"changed_keys": list(updates.keys())},
            request=request,
        )
        await db.commit()
    except AdminSettingsStorageError as exc:
        await db.rollback()
        logger.warning("SMTP 설정 저장 실패: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ADMIN_SETTINGS_UNAVAILABLE_DETAIL,
        ) from exc
    except Exception:
        await db.rollback()
        logger.exception("SMTP 설정 저장 또는 감사 기록 실패")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="SMTP 설정을 저장하지 못했습니다.")

    cache_refreshed = await refresh_admin_settings_cache(current)
    return {
        "message": "SMTP 설정이 저장되었습니다." if cache_refreshed else "SMTP 설정은 저장되었으나 캐시 갱신이 보류되었습니다.",
        "cache_refresh_pending": not cache_refreshed,
    }


@router.post("/email/test-smtp")
async def test_smtp(
    request: Request,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """현재 SMTP 설정으로 테스트 메일 발송 — 결과를 감사 로그에 남긴다."""
    subject, html_body = test_email_template(locale="ko")
    try:
        await send_email(admin.email, subject, html_body)
    except Exception as exc:
        logger.warning("SMTP 테스트 발송 실패 (admin_id=%s): %s", admin.id, type(exc).__name__)
        try:
            await record_audit(
                db,
                actor_id=str(admin.id),
                action="email.test",
                target_type="email",
                details={"success": False, "error_type": type(exc).__name__},
                request=request,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("SMTP 테스트 실패 감사 기록 저장 실패")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SMTP 테스트 메일 발송에 실패했습니다. 설정과 발송 로그를 확인하세요.",
        )

    try:
        await record_audit(
            db,
            actor_id=str(admin.id),
            action="email.test",
            target_type="email",
            details={"success": True},
            request=request,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("SMTP 테스트 성공 감사 기록 저장 실패")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="SMTP 테스트 결과를 저장하지 못했습니다.")
    return {"message": "테스트 메일 발송 요청이 완료되었습니다."}
