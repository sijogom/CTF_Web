import hmac
import logging
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.gamification import XP_TABLE, get_xp_for_level
from app.config import settings
from app.database import get_db
from app.email.sender import send_email
from app.email.templates import inquiry_notification_template
from app.models.competition import (
    Competition,
    CompetitionChallenge,
    CompetitionParticipant,
    CompetitionSolve,
    CompetitionTeamSolve,
)
from app.services import competition_team as team_service
from app.models.notification import Notification
from app.models.inquiry_notification import InquiryNotificationReceipt, is_safe_inquiry_access_path
from app.models.user import User
from app.models.point_transfer import MarketplacePointTransfer
from app.schemas.point_transfer import MarketplaceTransferRequest, MarketplaceTransferResponse
from app.utils.point_transfers import settle_marketplace_transfer, transfer_response
from app.utils.bans import get_active_ban
from app.utils.competition_access import require_competition_challenge_access
from app.utils.audit import record_audit
from app.utils.rich_content import sanitize_rich_html

logger = logging.getLogger(__name__)


def verify_internal_key(request: Request) -> None:
    """X-Internal-Key 헤더를 검증한다. 키가 설정되어 있으면 반드시 일치해야 한다."""
    if not settings.internal_api_key:
        if settings.is_production:
            logger.error(
                "INTERNAL_API_KEY가 비어있습니다. "
                "프로덕션 환경에서는 반드시 설정하세요. (path=%s)",
                request.url.path,
            )
            raise HTTPException(status_code=503, detail="내부 API 키 미설정")
        logger.warning(
            "INTERNAL_API_KEY 미설정 — 개발 환경에서 내부 API 인증을 건너뜁니다. (path=%s)",
            request.url.path,
        )
        return  # 개발 환경에서 키 미설정 시 통과
    key = request.headers.get("X-Internal-Key", "")
    if not hmac.compare_digest(key, settings.internal_api_key):
        raise HTTPException(status_code=403, detail="내부 API 인증 실패")


router = APIRouter(tags=["internal"], dependencies=[Depends(verify_internal_key)])


_INQUIRY_NOTIFICATION_LEASE = timedelta(minutes=5)
_INQUIRY_NOTIFICATION_RETRY_ERROR = "delivery_failed"


class InquiryNotificationRequest(BaseModel):
    """Community outbox에서 받는 최소 문의 알림 계약.

    답변·guest proof·HTML·외부 URL은 이 입력 계약에 존재하지 않으며, 추가 필드도
    허용하지 않는다. guest 이메일은 DB 영수증 또는 감사 로그에 기록하지 않는다.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    recipient_type: Literal["guest", "member"]
    recipient_email: EmailStr | None = None
    recipient_user_id: UUID | None = None
    kind: Literal["guest_access", "reply_available"]
    access_path: str = Field(min_length=1, max_length=2048)
    locale: str = Field(default="ko", min_length=2, max_length=10)

    @model_validator(mode="after")
    def validate_recipient(self):
        if self.recipient_type == "guest":
            if self.recipient_email is None or self.recipient_user_id is not None:
                raise ValueError("guest 수신자는 recipient_email만 지정해야 합니다.")
        elif self.recipient_user_id is None or self.recipient_email is not None:
            raise ValueError("member 수신자는 recipient_user_id만 지정해야 합니다.")
        return self

    @field_validator("access_path")
    @classmethod
    def validate_access_path(cls, value: str) -> str:
        if not is_safe_inquiry_access_path(value):
            raise ValueError("access_path는 /inquiry 아래의 안전한 상대 경로여야 합니다.")
        return value

    @field_validator("locale")
    @classmethod
    def normalize_locale(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("locale은 비어 있을 수 없습니다.")
        return normalized


def _inquiry_access_url(access_path: str) -> str:
    """이미 검증된 상대 경로를 Auth의 기존 frontend origin에 결합한다."""
    return f"{settings.frontend_url.rstrip('/')}{access_path}"


async def _record_inquiry_notification_audit(
    db: AsyncSession,
    receipt: InquiryNotificationReceipt,
    *,
    action: str,
) -> None:
    """이메일 주소, proof, 접근 경로, 메일 본문을 제외한 delivery 감사 기록."""
    await record_audit(
        db,
        actor_id=None,
        action=action,
        target_type="inquiry_notification_receipt",
        target_id=str(receipt.event_id),
        details={
            "status": receipt.status,
            "attempt_count": receipt.attempt_count,
        },
    )


async def _find_receipt(
    db: AsyncSession, event_id: UUID, *, for_update: bool = False
) -> InquiryNotificationReceipt | None:
    statement = select(InquiryNotificationReceipt).where(
        InquiryNotificationReceipt.event_id == event_id
    )
    if for_update:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


@router.get("/users/{user_id}/inquiry-contact")
async def inquiry_contact(user_id: UUID, db: AsyncSession = Depends(get_db)):
    """Community가 member 문의의 서버 권위 연락처만 사용하도록 한다."""
    user = (
        await db.execute(
            select(User.id, User.username, User.email).where(
                User.id == user_id,
                User.is_active.is_(True),
                User.email_verified.is_(True),
            )
        )
    ).one_or_none()
    if not user:
        return {"available": False}
    return {
        "available": True,
        "user_id": str(user.id),
        "username": user.username,
        "email": user.email,
    }


@router.post("/inquiry-notifications")
async def send_inquiry_notification(
    body: InquiryNotificationRequest,
    db: AsyncSession = Depends(get_db),
):
    """문의 outbox event를 단 한 개의 Auth delivery receipt로 처리한다.

    SMTP 전송 전에는 짧은 lease만 커밋한다. 재호출은 terminal receipt를 그대로
    반환하고, 실패 또는 만료된 lease는 같은 event의 재시도로 이어진다.
    """
    now = datetime.now(timezone.utc)
    receipt = await _find_receipt(db, body.event_id, for_update=True)

    if receipt and receipt.is_terminal:
        terminal_status = receipt.status
        await db.rollback()
        return {"status": terminal_status}

    if (
        receipt
        and receipt.status == "processing"
        and receipt.lease_expires_at is not None
        and receipt.lease_expires_at > now
    ):
        await db.rollback()
        raise HTTPException(status_code=409, detail="문의 알림이 이미 처리 중입니다. 나중에 재시도하세요.")

    if receipt is None:
        receipt = InquiryNotificationReceipt(
            event_id=body.event_id,
            status="processing",
            attempt_count=1,
            lease_expires_at=now + _INQUIRY_NOTIFICATION_LEASE,
        )
        db.add(receipt)
    else:
        receipt.status = "processing"
        receipt.attempt_count += 1
        receipt.lease_expires_at = now + _INQUIRY_NOTIFICATION_LEASE
        receipt.last_error = None

    try:
        await db.commit()
    except IntegrityError:
        # 동시에 같은 event를 최초 접수한 요청은 unique event_id 영수증을 기준으로
        # 하나만 전송한다. 승자 상태를 기다리도록 호출자에게 retryable 오류를 준다.
        await db.rollback()
        raced_receipt = await _find_receipt(db, body.event_id)
        if raced_receipt and raced_receipt.is_terminal:
            terminal_status = raced_receipt.status
            await db.rollback()
            return {"status": terminal_status}
        await db.rollback()
        raise HTTPException(status_code=409, detail="문의 알림이 이미 처리 중입니다. 나중에 재시도하세요.")

    # expire_on_commit=False이므로 receipt는 계속 사용할 수 있다. member 연락처는
    # 매 시도마다 현재 active+verified 계정에서만 서버 측으로 해석한다.
    recipient_email: str | None = None
    recipient_name: str | None = None
    if body.recipient_type == "member":
        user = (
            await db.execute(
                select(User).where(
                    User.id == body.recipient_user_id,
                    User.is_active.is_(True),
                    User.email_verified.is_(True),
                )
            )
        ).scalar_one_or_none()
        if user and user.email_notifications:
            recipient_email = user.email
            recipient_name = user.username
    else:
        recipient_email = str(body.recipient_email)

    if recipient_email is None:
        receipt.status = "skipped"
        receipt.lease_expires_at = None
        receipt.terminal_at = datetime.now(timezone.utc)
        receipt.last_error = None
        await _record_inquiry_notification_audit(
            db, receipt, action="inquiry_notification.skipped"
        )
        await db.commit()
        return {"status": "skipped"}

    try:
        subject, html_body = inquiry_notification_template(
            body.kind,
            _inquiry_access_url(body.access_path),
            recipient_name=recipient_name,
            locale=body.locale,
        )
        await send_email(recipient_email, subject, html_body)
    except Exception:
        # SMTP/템플릿 세부 오류에는 주소·SMTP 응답 등 민감값이 포함될 수 있어
        # 영수증, 감사 로그, 응답에 그대로 남기지 않는다.
        receipt.status = "retry"
        receipt.lease_expires_at = None
        receipt.last_error = _INQUIRY_NOTIFICATION_RETRY_ERROR
        await _record_inquiry_notification_audit(
            db, receipt, action="inquiry_notification.retry"
        )
        await db.commit()
        logger.warning("문의 알림 전송 재시도 예약: event_id=%s", body.event_id)
        raise HTTPException(status_code=503, detail="문의 알림 전송을 나중에 재시도하세요.")

    receipt.status = "sent"
    receipt.lease_expires_at = None
    receipt.terminal_at = datetime.now(timezone.utc)
    receipt.last_error = None
    await _record_inquiry_notification_audit(db, receipt, action="inquiry_notification.sent")
    await db.commit()
    return {"status": "sent"}


@router.get("/users/{user_id}/exists")
async def user_exists(user_id: str, db: AsyncSession = Depends(get_db)):
    """내부 서비스용 — 유저 존재 확인"""
    result = await db.execute(select(User.id).where(User.id == user_id))
    if result.scalar_one_or_none():
        return {"exists": True}
    return {"exists": False}


@router.get("/users/{user_id}/authorization")
async def user_authorization(user_id: str, db: AsyncSession = Depends(get_db)):
    """내부 서비스용 현재 계정 상태·역할 조회.

    하위 서비스는 JWT 안의 과거 role만으로 권한을 결정하지 않고 이 응답을
    기준으로 비활성화·역할 변경을 즉시 반영한다. 호출자는 router 의존성으로
    X-Internal-Key 검증을 이미 통과해야 한다.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return {"exists": False, "is_active": False, "role": None, "ban_active": False}
    active_ban = await get_active_ban(db, user.id)
    authorized = bool(user.is_active) and active_ban is None
    return {
        "exists": True,
        "is_active": authorized,
        "role": (user.role or "user") if authorized else None,
        "ban_active": active_ban is not None,
    }


@router.post("/users/batch")
async def batch_users(ids: list[str], db: AsyncSession = Depends(get_db)):
    """내부 서비스용 — 유저 정보 일괄 조회"""
    result = await db.execute(
        select(User.id, User.username, User.avatar_url).where(User.id.in_(ids))
    )
    users = [{"id": str(r.id), "username": r.username, "avatar_url": r.avatar_url} for r in result.all()]
    return users


# ──── 포인트 관련 내부 API ────


class PointsRequest(BaseModel):
    user_id: str
    amount: int = Field(..., gt=0)


@router.post("/points/transfers", response_model=MarketplaceTransferResponse)
async def marketplace_transfer(
    body: MarketplaceTransferRequest,
    db: AsyncSession = Depends(get_db),
):
    return await settle_marketplace_transfer(db, body)


@router.get("/points/transfers/{operation_id}", response_model=MarketplaceTransferResponse)
async def marketplace_transfer_status(
    operation_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    transfer = (await db.execute(select(MarketplacePointTransfer).where(
        MarketplacePointTransfer.operation_id == operation_id,
    ))).scalar_one_or_none()
    if transfer is None:
        raise HTTPException(status_code=404, detail="이체 작업을 찾을 수 없습니다.")
    return transfer_response(transfer)


@router.get("/points/{user_id}")
async def get_points(user_id: str, db: AsyncSession = Depends(get_db)):
    """내부 서비스용 — 사용자 포인트 조회."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    return {"user_id": str(user.id), "points": user.points}


@router.post("/points/add")
async def add_points(body: PointsRequest, db: AsyncSession = Depends(get_db)):
    """내부 서비스용 — 포인트 지급 (원자적 증가)."""
    result = await db.execute(
        update(User)
        .where(User.id == body.user_id)
        .values(points=User.points + body.amount)
        .returning(User.id, User.points)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    await db.commit()
    return {"user_id": str(row.id), "points": row.points}


@router.post("/points/deduct")
async def deduct_points(body: PointsRequest, db: AsyncSession = Depends(get_db)):
    """내부 서비스용 — 포인트 차감 (원자적 감소, 잔고 부족 시 거부)."""
    result = await db.execute(
        update(User)
        .where(User.id == body.user_id, User.points >= body.amount)
        .values(points=User.points - body.amount)
        .returning(User.id, User.points)
    )
    row = result.one_or_none()
    if not row:
        # 사용자 존재 여부 확인
        user_check = await db.execute(select(User.id).where(User.id == body.user_id))
        if not user_check.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
        raise HTTPException(status_code=400, detail="포인트가 부족합니다.")
    await db.commit()
    return {"user_id": str(row.id), "points": row.points}


class RewardRequest(BaseModel):
    user_id: str
    points: int = Field(0, ge=0)
    xp: int = Field(0, ge=0)


@router.post("/reward")
async def reward_user(body: RewardRequest, db: AsyncSession = Depends(get_db)):
    """내부 서비스용 — 포인트 + XP 동시 지급 (원자적 증가)."""
    # 원자적 증가로 레이스 컨디션 방지
    values = {}
    if body.points > 0:
        values["points"] = User.points + body.points
    if body.xp > 0:
        values["xp"] = User.xp + body.xp

    if not values:
        # 둘 다 0이면 현재 상태만 반환
        result = await db.execute(select(User).where(User.id == body.user_id))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
        return {"user_id": str(user.id), "points": user.points, "xp": user.xp, "level": user.level}

    result = await db.execute(
        update(User)
        .where(User.id == body.user_id)
        .values(**values)
        .returning(User.id, User.points, User.xp, User.level)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")

    # 레벨 업 계산 — gamification.py의 XP_TABLE 기준
    new_level = row.level
    while row.xp >= get_xp_for_level(new_level + 1):
        new_level += 1

    if new_level > row.level:
        await db.execute(
            update(User).where(User.id == body.user_id).values(level=new_level)
        )

    await db.commit()
    return {
        "user_id": str(row.id),
        "points": row.points,
        "xp": row.xp,
        "level": max(row.level, new_level),
    }


# ──── 대회 점수 업데이트 내부 API ────


class CompetitionScoreRequest(BaseModel):
    competition_id: str
    user_id: str
    score_delta: int = Field(..., gt=0, description="추가할 점수 (양수)")


@router.post("/competition/score")
async def update_competition_score(
    body: CompetitionScoreRequest, db: AsyncSession = Depends(get_db)
):
    """내부 서비스용 — 대회 참가자 점수 업데이트.

    learn-service가 문제 풀이 시 호출하여 CompetitionParticipant.score를
    원자적으로 증가시킨다.
    """
    result = await db.execute(
        update(CompetitionParticipant)
        .where(
            CompetitionParticipant.competition_id == body.competition_id,
            CompetitionParticipant.user_id == body.user_id,
        )
        .values(score=CompetitionParticipant.score + body.score_delta)
        .returning(
            CompetitionParticipant.competition_id,
            CompetitionParticipant.user_id,
            CompetitionParticipant.score,
        )
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(
            status_code=404,
            detail="해당 대회의 참가자를 찾을 수 없습니다.",
        )
    await db.commit()
    logger.info(
        "대회 점수 업데이트: competition=%s user=%s delta=%d new_score=%d",
        body.competition_id, body.user_id, body.score_delta, row.score,
    )
    return {
        "competition_id": str(row.competition_id),
        "user_id": str(row.user_id),
        "score": row.score,
    }


# ──── 대회 풀이 기록 내부 API ────


class CompetitionSolveRequest(BaseModel):
    competition_id: str
    user_id: str
    challenge_id: int
    accepted_at: datetime


class CompetitionAccessRequest(BaseModel):
    """대회 문제 접근 검증에 필요한 최소 내부 요청 계약."""

    user_id: str
    challenge_id: int
    purpose: Literal["view", "submit", "environment"]


@router.get("/competitions/{comp_id}/status")
async def competition_status(comp_id: str, db: AsyncSession = Depends(get_db)):
    """내부 서비스용 — 대회 진행 상태 반환."""
    from datetime import datetime, timezone
    result = await db.execute(select(Competition).where(Competition.id == comp_id))
    comp = result.scalar_one_or_none()
    if not comp or comp.is_deleted:
        raise HTTPException(status_code=404, detail="대회를 찾을 수 없습니다.")
    now = datetime.now(timezone.utc)
    if now < comp.start_at:
        status = "upcoming"
    elif now >= comp.end_at:
        status = "ended"
    else:
        status = "ongoing"
    return {"status": status}


@router.post("/competitions/{comp_id}/access")
async def competition_challenge_access(
    comp_id: str,
    body: CompetitionAccessRequest,
    db: AsyncSession = Depends(get_db),
):
    """내부 서비스용 대회 문제 접근 확인 API.

    브라우저가 직접 호출하지 않는다. `purpose`는 허용되는 동작을 명시해 호출 경로를
    문서화하며, 각 서비스가 같은 접근 조건을 재사용하도록 한다.
    """
    await require_competition_challenge_access(
        competition_id=comp_id,
        user_id=body.user_id,
        challenge_id=body.challenge_id,
        db=db,
    )
    return {"allowed": True, "purpose": body.purpose}


def _first_blood_notification(username: str, comp_title: str, challenge_id: int) -> Notification:
    content = sanitize_rich_html(
        f"<p>{escape(comp_title, quote=False)} — Challenge #{challenge_id}</p>",
        field_name="대회 풀이 알림 내용",
        max_visible_chars=5000,
    )
    return Notification(title=f"First Blood! {username}", content=content, type="toast")


async def _record_team_competition_solve(body, db, comp):
    try:
        result = await team_service.record_team_solve(
            db, UUID(body.competition_id), UUID(body.user_id), body.challenge_id, body.accepted_at,
        )
    except team_service.CompetitionTeamError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error
    # 서비스가 유지하는 대회 잠금 안에서 최초 팀 풀이와 알림도 함께 확정한다.
    if result.team_inserted:
        count_result = await db.execute(
            select(func.count()).select_from(CompetitionTeamSolve).where(
                CompetitionTeamSolve.competition_id == body.competition_id,
                CompetitionTeamSolve.challenge_id == body.challenge_id,
            )
        )
        if count_result.scalar() == 1:
            username = (await db.execute(select(User.username).where(User.id == body.user_id))).scalar() or "unknown"
            db.add(_first_blood_notification(username, comp.title or "", body.challenge_id))
    await db.commit()
    return {"status": "recorded" if result.personal_inserted else "already_recorded"}


@router.post("/competition/solve")
async def record_competition_solve(
    body: CompetitionSolveRequest, db: AsyncSession = Depends(get_db)
):
    """내부 서비스용 — 대회 내 챌린지 풀이 기록.

    learn-service가 대회 컨텍스트에서 문제를 풀면 호출한다. accepted_at은 Learn DB에
    이미 저장된 정답 제출 시각이며, Auth 일시 장애 뒤 재시도해도 원래 대회 시간과
    First Blood 순서를 보존한다. 중복 풀이는 성공으로 취급한다.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    comp = await require_competition_challenge_access(
        competition_id=body.competition_id,
        user_id=body.user_id,
        challenge_id=body.challenge_id,
        db=db,
        accepted_at=body.accepted_at,
    )
    if comp is not None and team_service.is_fixed_roster_team(comp):
        return await _record_team_competition_solve(body, db, comp)

    # INSERT ON CONFLICT DO NOTHING (중복 풀이 무시)
    stmt = pg_insert(CompetitionSolve).values(
        competition_id=body.competition_id,
        user_id=body.user_id,
        challenge_id=body.challenge_id,
        solved_at=body.accepted_at,
    ).on_conflict_do_nothing()
    result = await db.execute(stmt)
    await db.commit()

    if result.rowcount == 0:
        # Learn 서비스가 Auth 응답을 받기 전에 연결이 끊겨 재시도한 경우에도
        # 같은 대회 풀이를 성공으로 취급한다. 복수 점수·알림을 만들지 않는다.
        return {"status": "already_recorded"}

    logger.info(
        "대회 풀이 기록: competition=%s user=%s challenge=%d",
        body.competition_id, body.user_id, body.challenge_id,
    )

    # First Blood 체크: 해당 챌린지의 첫 풀이인 경우 알림 생성
    solve_count_result = await db.execute(
        select(func.count()).select_from(CompetitionSolve).where(
            CompetitionSolve.competition_id == body.competition_id,
            CompetitionSolve.challenge_id == body.challenge_id,
        )
    )
    if solve_count_result.scalar() == 1:
        # 사용자 이름 조회
        user_result = await db.execute(select(User.username).where(User.id == body.user_id))
        username = user_result.scalar() or "unknown"
        # 대회 이름 조회
        comp_result = await db.execute(
            select(Competition.title).where(Competition.id == body.competition_id)
        )
        comp_title = comp_result.scalar() or ""

        db.add(_first_blood_notification(username, comp_title, body.challenge_id))
        await db.commit()

    return {"status": "recorded"}
