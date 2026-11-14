"""1:1 문의 API: 소유권 기반 조회, 비회원 email proof, durable outbox."""

from __future__ import annotations

import hmac
from datetime import timedelta
from typing import Literal
from uuid import UUID as PyUUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import (
    get_current_user_id,
    get_current_username,
    get_optional_user_id,
    require_role,
)
from app.api.internal import verify_internal_key
from app.config import settings
from app.database import get_db
from app.models.inquiry import (
    Inquiry,
    InquiryAccessChallenge,
    InquiryAccessSession,
    InquiryEvent,
    InquiryNotificationOutbox,
    InquiryReply,
)
from app.utils.auth_client import get_inquiry_contact
from app.utils.inquiry_ticket import (
    GUEST_ACCESS_PROOF_TTL,
    GUEST_SESSION_TTL,
    issue_opaque_token,
    token_digest,
    utc_now,
    verify_access_proof,
)
from app.utils.safe_html import sanitize_rich_content
from app.workers.inquiry_outbox import (
    claim_inquiry_notifications,
    complete_inquiry_notification,
)


router = APIRouter(prefix="/inquiries", tags=["문의"])

_VALID_STATUSES = frozenset({"open", "answered", "closed"})
_ALLOWED_STATUS_TRANSITIONS = {
    "open": frozenset({"closed"}),
    "answered": frozenset({"open", "closed"}),
    "closed": frozenset({"open"}),
}
_SUPPORTED_LOCALES = frozenset({"ko", "en", "ja", "zh"})
_GUEST_CREATE_LIMIT = 3
_MEMBER_CREATE_LIMIT = 10
_CREATE_LIMIT_WINDOW = timedelta(hours=1)
_GUEST_ACCESS_REQUEST_LIMIT = 3
_GUEST_ACCESS_REQUEST_WINDOW = timedelta(minutes=15)


class InquiryCreate(BaseModel):
    """회원/비회원 공용 접수 입력. 회원 contact는 서버가 대체한다."""

    email: EmailStr | None = Field(None, max_length=254)
    name: str | None = Field(None, min_length=1, max_length=100)
    subject: str = Field(..., min_length=1, max_length=300)
    content: str
    locale: str = Field("ko", max_length=10)

    @field_validator("name", "subject")
    @classmethod
    def non_blank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("공백만으로는 문의 내용을 제출할 수 없습니다.")
        return cleaned

    @field_validator("locale")
    @classmethod
    def supported_locale(cls, value: str) -> str:
        return value if value in _SUPPORTED_LOCALES else "ko"

    @field_validator("content")
    @classmethod
    def sanitize_content(cls, value: str) -> str:
        return sanitize_rich_content(
            value,
            field_name="문의 내용",
            max_visible_chars=5000,
        )


class InquiryReplyCreate(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def non_blank_content(cls, value: str) -> str:
        return sanitize_rich_content(
            value,
            field_name="답변 내용",
            max_visible_chars=5000,
        )


class GuestAccessRequest(BaseModel):
    email: EmailStr = Field(..., max_length=254)


class GuestAccessExchange(BaseModel):
    challenge_id: PyUUID
    proof: str = Field(..., min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")


class OutboxClaimRequest(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=100)
    limit: int = Field(1, ge=1, le=10)


class OutboxCompleteRequest(BaseModel):
    lease_token: str = Field(..., min_length=32, max_length=255)
    result: Literal["sent", "skipped", "retry"]
    error: str | None = Field(None, max_length=500)


def _record_event(
    db: AsyncSession,
    *,
    inquiry: Inquiry,
    actor_type: str,
    actor_id: PyUUID | None,
    action: str,
    details: dict | None = None,
) -> None:
    db.add(
        InquiryEvent(
            inquiry_id=inquiry.id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            details=details,
        )
    )


def _parse_inquiry_id(inquiry_id: str) -> PyUUID:
    try:
        return PyUUID(inquiry_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효하지 않은 문의 ID입니다.") from exc


def _has_auth_attempt(request: Request) -> bool:
    return bool(request.cookies.get("access_token") or request.headers.get("authorization"))


def _require_access_proof_secret() -> str:
    """Never issue a guest capability if the existing internal trust secret is absent."""
    if not settings.internal_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="비회원 문의 조회 서비스를 일시적으로 사용할 수 없습니다.",
        )
    return settings.internal_api_key


def _guest_session_cookie_name(inquiry_id: PyUUID) -> str:
    return f"inquiry_guest_session_{inquiry_id.hex}"


async def _enforce_create_rate_limit(
    db: AsyncSession,
    *,
    user_id: PyUUID | None,
    email: str,
) -> None:
    cutoff = utc_now() - _CREATE_LIMIT_WINDOW
    if user_id is None:
        query = select(func.count()).select_from(Inquiry).where(
            func.lower(Inquiry.email) == email.lower(),
            Inquiry.created_at >= cutoff,
        )
        limit = _GUEST_CREATE_LIMIT
    else:
        query = select(func.count()).select_from(Inquiry).where(
            Inquiry.user_id == user_id,
            Inquiry.created_at >= cutoff,
        )
        limit = _MEMBER_CREATE_LIMIT
    current = (await db.execute(query)).scalar() or 0
    if current >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="문의 접수 요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
            headers={"Retry-After": str(int(_CREATE_LIMIT_WINDOW.total_seconds()))},
        )


def _enqueue_notification(
    db: AsyncSession,
    *,
    inquiry: Inquiry,
    recipient_type: str,
    recipient_user_id: PyUUID | None,
    kind: str,
    dedupe_key: str,
    reply_id: PyUUID | None = None,
    challenge_id: PyUUID | None = None,
    locale: str = "ko",
) -> InquiryNotificationOutbox:
    outbox = InquiryNotificationOutbox(
        inquiry_id=inquiry.id,
        reply_id=reply_id,
        challenge_id=challenge_id,
        recipient_type=recipient_type,
        recipient_user_id=recipient_user_id,
        kind=kind,
        locale=locale if locale in _SUPPORTED_LOCALES else "ko",
        dedupe_key=dedupe_key,
    )
    db.add(outbox)
    _record_event(
        db,
        inquiry=inquiry,
        actor_type="system",
        actor_id=None,
        action="inquiry.notification_enqueued",
        details={"kind": kind, "dedupe_key": dedupe_key},
    )
    return outbox


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_inquiry(
    body: InquiryCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user_id: PyUUID | None = Depends(get_optional_user_id),
):
    """Create a ticket while resolving member contact information server-side."""
    if user_id is not None:
        contact = await get_inquiry_contact(str(user_id))
        if contact is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="인증된 문의 연락처를 확인할 수 없습니다.",
            )
        email = contact.email
        name = contact.username
        actor_type = "user"
    else:
        if _has_auth_attempt(request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="유효하지 않은 인증 정보입니다. 다시 로그인해 주세요.",
            )
        if body.email is None or body.name is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="비회원 문의에는 이름과 이메일이 필요합니다.",
            )
        email = str(body.email)
        name = body.name
        actor_type = "guest"

    await _enforce_create_rate_limit(db, user_id=user_id, email=email)
    inquiry = Inquiry(
        user_id=user_id,
        email=email,
        name=name,
        locale=body.locale,
        subject=body.subject,
        content=body.content,
    )
    db.add(inquiry)
    await db.flush()
    _record_event(
        db,
        inquiry=inquiry,
        actor_type=actor_type,
        actor_id=user_id,
        action="inquiry.created",
    )
    await db.commit()
    return {
        "id": str(inquiry.id),
        "status": inquiry.status,
        "guest_access_required": user_id is None,
        "message": "문의가 접수되었습니다.",
    }


@router.get("/my")
async def my_inquiries(
    db: AsyncSession = Depends(get_db),
    user_id: PyUUID = Depends(get_current_user_id),
):
    """Return only the authenticated member's own inquiry summaries and replies."""
    result = await db.execute(
        select(Inquiry)
        .options(selectinload(Inquiry.replies))
        .where(Inquiry.user_id == user_id)
        .order_by(Inquiry.created_at.desc())
    )
    inquiries = result.scalars().all()
    return [
        {
            "id": str(inquiry.id),
            "subject": inquiry.subject,
            "status": inquiry.status,
            "created_at": inquiry.created_at.isoformat() if inquiry.created_at else None,
            "replies": [
                {
                    "admin_name": reply.admin_name,
                    "content": reply.content,
                    "created_at": reply.created_at.isoformat() if reply.created_at else None,
                }
                for reply in inquiry.replies
            ],
        }
        for inquiry in inquiries
    ]


@router.post("/guest-access/exchange", status_code=status.HTTP_204_NO_CONTENT)
async def exchange_guest_access(
    body: GuestAccessExchange,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Exchange an email-link proof for a short-lived ticket-scoped cookie."""
    secret = _require_access_proof_secret()
    current = utc_now()
    record = await db.execute(
        select(InquiryAccessChallenge)
        .options(selectinload(InquiryAccessChallenge.inquiry))
        .where(InquiryAccessChallenge.id == body.challenge_id)
        .with_for_update()
    )
    challenge = record.scalar_one_or_none()
    if challenge is None or challenge.used_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 문의 접근 증명입니다.")
    if challenge.expires_at <= current:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="문의 접근 증명이 만료되었습니다.")
    if not verify_access_proof(challenge.id, challenge.expires_at, body.proof, secret, now=current):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 문의 접근 증명입니다.")
    if challenge.inquiry.user_id is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 문의 접근 증명입니다.")

    raw_token = issue_opaque_token()
    session = InquiryAccessSession(
        inquiry_id=challenge.inquiry_id,
        token_digest=token_digest(raw_token),
        expires_at=current + GUEST_SESSION_TTL,
    )
    challenge.used_at = current
    db.add(session)
    _record_event(
        db,
        inquiry=challenge.inquiry,
        actor_type="guest",
        actor_id=None,
        action="inquiry.guest_access_verified",
        details={"challenge_id": str(challenge.id)},
    )
    await db.commit()
    response.set_cookie(
        key=_guest_session_cookie_name(challenge.inquiry_id),
        value=raw_token,
        max_age=int(GUEST_SESSION_TTL.total_seconds()),
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )
    return None


@router.post("/internal/outbox/claim")
async def claim_outbox(
    body: OutboxClaimRequest,
    db: AsyncSession = Depends(get_db),
    _internal: None = Depends(verify_internal_key),
):
    """Lease durable notification commands; no SMTP is called from Community."""
    secret = _require_access_proof_secret()
    claims = await claim_inquiry_notifications(
        db,
        worker_id=body.worker_id,
        limit=body.limit,
        proof_secret=secret,
    )
    await db.commit()
    return {"items": [claim.as_dict() for claim in claims]}


@router.post("/internal/outbox/{outbox_id}/complete")
async def complete_outbox(
    outbox_id: PyUUID,
    body: OutboxCompleteRequest,
    db: AsyncSession = Depends(get_db),
    _internal: None = Depends(verify_internal_key),
):
    """Complete a claim after the external runner has called Auth."""
    try:
        final_state = await complete_inquiry_notification(
            db,
            outbox_id=outbox_id,
            lease_token=body.lease_token,
            result=body.result,
            error=body.error,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="알림 작업을 찾을 수 없습니다.") from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="알림 작업 lease가 유효하지 않습니다.") from exc
    await db.commit()
    return {"state": final_state}


@router.post("/{inquiry_id}/guest-access/request", status_code=status.HTTP_202_ACCEPTED)
async def request_guest_access(
    inquiry_id: str,
    body: GuestAccessRequest,
    db: AsyncSession = Depends(get_db),
):
    """Queue an email proof without exposing ticket or email existence."""
    secret = _require_access_proof_secret()
    inquiry_uuid = _parse_inquiry_id(inquiry_id)
    current = utc_now()
    generic_response = {"message": "문의 접근 안내를 이메일로 보냈습니다."}
    record = await db.execute(
        select(Inquiry).where(Inquiry.id == inquiry_uuid).with_for_update()
    )
    inquiry = record.scalar_one_or_none()
    if inquiry is None or inquiry.user_id is not None:
        return generic_response
    if not hmac.compare_digest(inquiry.email.lower(), str(body.email).lower()):
        return generic_response

    count = await db.execute(
        select(func.count())
        .select_from(InquiryAccessChallenge)
        .where(
            InquiryAccessChallenge.inquiry_id == inquiry.id,
            InquiryAccessChallenge.purpose == "guest_access",
            InquiryAccessChallenge.created_at >= current - _GUEST_ACCESS_REQUEST_WINDOW,
        )
    )
    if (count.scalar() or 0) >= _GUEST_ACCESS_REQUEST_LIMIT:
        return generic_response

    challenge = InquiryAccessChallenge(
        inquiry_id=inquiry.id,
        purpose="guest_access",
        expires_at=current + GUEST_ACCESS_PROOF_TTL,
    )
    db.add(challenge)
    await db.flush()
    outbox = _enqueue_notification(
        db,
        inquiry=inquiry,
        recipient_type="guest",
        recipient_user_id=None,
        kind="guest_access",
        challenge_id=challenge.id,
        dedupe_key=f"guest-access:{challenge.id}",
        locale=inquiry.locale,
    )
    await db.flush()
    _record_event(
        db,
        inquiry=inquiry,
        actor_type="guest",
        actor_id=None,
        action="inquiry.guest_access_requested",
        details={"outbox_id": str(outbox.id)},
    )
    # The secret is required above so a durable row is never queued without a proof signer.
    del secret
    await db.commit()
    return generic_response


@router.get("/{inquiry_id}/viewer")
async def view_inquiry_as_owner(
    inquiry_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user_id: PyUUID | None = Depends(get_optional_user_id),
):
    """Show a ticket only to its member owner or its ticket-scoped guest session."""
    inquiry_uuid = _parse_inquiry_id(inquiry_id)
    if user_id is None and _has_auth_attempt(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 인증 정보입니다.")
    result = await db.execute(
        select(Inquiry)
        .options(selectinload(Inquiry.replies))
        .where(Inquiry.id == inquiry_uuid)
    )
    inquiry = result.scalar_one_or_none()
    if inquiry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="문의를 찾을 수 없습니다.")

    allowed = user_id is not None and inquiry.user_id == user_id
    if not allowed and inquiry.user_id is None:
        raw_token = request.cookies.get(_guest_session_cookie_name(inquiry.id))
        if raw_token:
            session_result = await db.execute(
                select(InquiryAccessSession).where(
                    InquiryAccessSession.inquiry_id == inquiry.id,
                    InquiryAccessSession.token_digest == token_digest(raw_token),
                    InquiryAccessSession.expires_at > utc_now(),
                    InquiryAccessSession.revoked_at.is_(None),
                )
            )
            allowed = session_result.scalar_one_or_none() is not None
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="문의 조회 권한이 없습니다.")

    return {
        "id": str(inquiry.id),
        "subject": inquiry.subject,
        "content": inquiry.content,
        "status": inquiry.status,
        "created_at": inquiry.created_at.isoformat() if inquiry.created_at else None,
        "replies": [
            {
                "admin_name": reply.admin_name,
                "content": reply.content,
                "created_at": reply.created_at.isoformat() if reply.created_at else None,
            }
            for reply in inquiry.replies
        ],
    }


@router.get("")
async def list_inquiries(
    status_filter: str = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    query = select(Inquiry)
    if status_filter:
        if status_filter not in _VALID_STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효하지 않은 상태입니다.")
        query = query.where(Inquiry.status == status_filter)
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0
    result = await db.execute(query.order_by(Inquiry.created_at.desc()).offset((page - 1) * size).limit(size))
    inquiries = result.scalars().all()
    return {
        "items": [
            {
                "id": str(inquiry.id),
                "user_id": str(inquiry.user_id) if inquiry.user_id else None,
                "email": inquiry.email,
                "name": inquiry.name,
                "subject": inquiry.subject,
                "status": inquiry.status,
                "created_at": inquiry.created_at.isoformat() if inquiry.created_at else None,
            }
            for inquiry in inquiries
        ],
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/{inquiry_id}")
async def get_inquiry(
    inquiry_id: str,
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    inquiry_uuid = _parse_inquiry_id(inquiry_id)
    result = await db.execute(
        select(Inquiry)
        .options(selectinload(Inquiry.replies), selectinload(Inquiry.events), selectinload(Inquiry.notification_outbox))
        .where(Inquiry.id == inquiry_uuid)
    )
    inquiry = result.scalar_one_or_none()
    if not inquiry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="문의를 찾을 수 없습니다.")
    return {
        "id": str(inquiry.id),
        "user_id": str(inquiry.user_id) if inquiry.user_id else None,
        "email": inquiry.email,
        "name": inquiry.name,
        "subject": inquiry.subject,
        "content": inquiry.content,
        "status": inquiry.status,
        "created_at": inquiry.created_at.isoformat() if inquiry.created_at else None,
        "replies": [
            {
                "id": str(reply.id),
                "admin_name": reply.admin_name,
                "content": reply.content,
                "created_at": reply.created_at.isoformat() if reply.created_at else None,
            }
            for reply in inquiry.replies
        ],
        "events": [
            {
                "actor_type": event.actor_type,
                "action": event.action,
                "created_at": event.created_at.isoformat() if event.created_at else None,
            }
            for event in inquiry.events
        ],
        "notification_summary": {
            "queued": sum(item.state in {"pending", "processing", "retry"} for item in inquiry.notification_outbox),
            "sent": sum(item.state == "sent" for item in inquiry.notification_outbox),
            "skipped": sum(item.state == "skipped" for item in inquiry.notification_outbox),
        },
    }


@router.post("/{inquiry_id}/reply", status_code=status.HTTP_201_CREATED)
async def reply_inquiry(
    inquiry_id: str,
    body: InquiryReplyCreate,
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
    admin_id: PyUUID = Depends(get_current_user_id),
    admin_name: str = Depends(get_current_username),
):
    inquiry_uuid = _parse_inquiry_id(inquiry_id)
    result = await db.execute(select(Inquiry).where(Inquiry.id == inquiry_uuid).with_for_update())
    inquiry = result.scalar_one_or_none()
    if not inquiry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="문의를 찾을 수 없습니다.")
    if inquiry.status == "closed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="종료된 문의는 재개한 뒤 답변할 수 있습니다.")

    reply = InquiryReply(
        inquiry_id=inquiry.id,
        admin_id=admin_id,
        admin_name=admin_name,
        content=body.content,
    )
    db.add(reply)
    await db.flush()
    previous_status = inquiry.status
    inquiry.status = "answered"

    if inquiry.user_id is None:
        _require_access_proof_secret()
        challenge = InquiryAccessChallenge(
            inquiry_id=inquiry.id,
            purpose="reply_access",
            expires_at=utc_now() + GUEST_ACCESS_PROOF_TTL,
        )
        db.add(challenge)
        await db.flush()
        outbox = _enqueue_notification(
            db,
            inquiry=inquiry,
            recipient_type="guest",
            recipient_user_id=None,
            kind="reply_available",
            reply_id=reply.id,
            challenge_id=challenge.id,
            dedupe_key=f"reply:{reply.id}:guest",
            locale=inquiry.locale,
        )
    else:
        outbox = _enqueue_notification(
            db,
            inquiry=inquiry,
            recipient_type="member",
            recipient_user_id=inquiry.user_id,
            kind="reply_available",
            reply_id=reply.id,
            dedupe_key=f"reply:{reply.id}:member:{inquiry.user_id}",
            locale=inquiry.locale,
        )
    await db.flush()
    _record_event(
        db,
        inquiry=inquiry,
        actor_type="admin",
        actor_id=admin_id,
        action="inquiry.replied",
        details={
            "from_status": previous_status,
            "to_status": inquiry.status,
            "reply_id": str(reply.id),
            "outbox_id": str(outbox.id),
        },
    )
    await db.commit()
    return {
        "message": "답변이 등록되었습니다.",
        "status": inquiry.status,
        "notification": {"state": "queued", "event_id": str(outbox.id)},
    }


@router.patch("/{inquiry_id}/status")
async def update_inquiry_status(
    inquiry_id: str,
    status_value: str = Query(..., alias="status"),
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
    admin_id: PyUUID = Depends(get_current_user_id),
):
    if status_value not in _VALID_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효하지 않은 상태입니다.")
    inquiry_uuid = _parse_inquiry_id(inquiry_id)
    result = await db.execute(select(Inquiry).where(Inquiry.id == inquiry_uuid).with_for_update())
    inquiry = result.scalar_one_or_none()
    if not inquiry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="문의를 찾을 수 없습니다.")
    if status_value == inquiry.status:
        return {"message": "이미 해당 상태입니다.", "status": inquiry.status}
    if status_value not in _ALLOWED_STATUS_TRANSITIONS[inquiry.status]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="현재 상태에서 요청한 상태로 변경할 수 없습니다.",
        )
    previous_status = inquiry.status
    inquiry.status = status_value
    _record_event(
        db,
        inquiry=inquiry,
        actor_type="admin",
        actor_id=admin_id,
        action="inquiry.status_changed",
        details={"from_status": previous_status, "to_status": status_value},
    )
    await db.commit()
    return {"message": "상태가 변경되었습니다.", "status": inquiry.status}
