"""서버 강제 3단계 모더레이션 확인 API.

1. 대상 영향 조회
2. 사유와 정확한 대상 확인명을 검증한 뒤 단기 일회용 토큰 발급
3. 토큰을 소비해 숨김 또는 복구를 실행하고 감사 이벤트를 남김
"""

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id, require_role
from app.database import get_db
from app.models.marketplace import MarketplaceComment, MarketplaceItem
from app.models.moderation import ModerationConfirmation, ModerationEvent
from app.models.post import Comment, Post


TargetType = Literal["post", "comment", "marketplace_item", "marketplace_comment"]
ModerationAction = Literal["hide", "restore"]

CONFIRMATION_TOKEN_TTL = timedelta(minutes=10)
TOKEN_BYTES = 32

router = APIRouter(tags=["moderation"])


@dataclass
class ResolvedModerationTarget:
    target_type: TargetType
    target_id: UUID
    entity: Post | Comment | MarketplaceItem | MarketplaceComment
    target_name: str
    target_preview: str | None
    target_author: str | None
    is_hidden: bool
    is_deleted: bool
    target_fingerprint: str
    impact_scope: str


class ModerationTargetImpactResponse(BaseModel):
    target_type: TargetType
    target_id: UUID
    target_name: str
    target_preview: str | None
    target_author: str | None
    is_hidden: bool
    is_deleted: bool
    impact_scope: str
    related_counts: dict[str, int]
    available_actions: list[ModerationAction]


class ModerationConfirmationRequest(BaseModel):
    target_type: TargetType
    target_id: UUID
    action: ModerationAction
    target_name: str = Field(..., min_length=1, max_length=300)
    reason: str = Field(..., min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _require_nonblank_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("모더레이션 사유를 입력해야 합니다.")
        return normalized


class ModerationConfirmationResponse(BaseModel):
    confirmation_token: str
    expires_at: datetime
    target_type: TargetType
    target_id: UUID
    target_name: str
    impact_scope: str
    action: ModerationAction


class ModerationActionRequest(BaseModel):
    target_type: TargetType
    target_id: UUID
    action: ModerationAction
    confirmation_token: str = Field(..., min_length=32, max_length=256)


class ModerationActionResponse(BaseModel):
    event_id: UUID
    target_type: TargetType
    target_id: UUID
    action: ModerationAction
    is_hidden: bool
    created_at: datetime


class ModerationEventResponse(BaseModel):
    id: UUID
    confirmation_id: UUID
    target_type: TargetType
    target_id: UUID
    target_name: str
    impact_scope: str
    action: ModerationAction
    reason: str
    actor_id: UUID
    actor_role: str
    prior_hidden: bool
    resulting_hidden: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ModerationEventListResponse(BaseModel):
    items: list[ModerationEventResponse]
    total: int
    page: int
    size: int


def confirmation_token_digest(raw_token: str) -> str:
    """원문 토큰을 저장하지 않기 위한 SHA-256 다이제스트를 계산한다."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def confirmation_is_expired(expires_at: datetime, now: datetime | None = None) -> bool:
    """UTC 기준 만료 여부를 계산하는 순수 helper."""
    current = now or datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return expires_at <= current


def direct_delete_policy(actor_role: str | None, is_owner: bool) -> Literal[
    "allow_owner", "requires_moderation", "forbidden"
]:
    """기존 DELETE 경로의 역할별 처리 방침을 fail-closed로 결정한다.

    일반 사용자는 본인 콘텐츠만 기존 삭제 계약을 계속 사용할 수 있다.
    admin/moderator는 본인 콘텐츠를 삭제하려는 경우에도 권한으로 감사·복구
    절차를 우회할 수 없도록, 항상 확인 토큰 기반 모더레이션 hide/restore
    경로를 사용해야 한다.
    """
    if actor_role in {"admin", "moderator"}:
        return "requires_moderation"
    if is_owner:
        return "allow_owner"
    return "forbidden"


def assert_direct_owner_delete_allowed(actor_role: str | None, is_owner: bool) -> None:
    """기존 DELETE에서 관리자 우회와 타인 삭제를 서버 측에서 거부한다."""
    policy = direct_delete_policy(actor_role, is_owner)
    if policy == "requires_moderation":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "관리자·모더레이터의 콘텐츠 조치는 직접 삭제할 수 없습니다. "
                "영향 범위 확인, 사유 입력, 대상명 확인을 거친 모더레이션 "
                "숨김/복구 경로를 사용하세요."
            ),
        )
    if policy == "forbidden":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="삭제 권한이 없습니다.")


def _constant_time_name_match(submitted: str, expected: str) -> bool:
    return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))


def _display_name(target_type: TargetType, entity: Post | Comment | MarketplaceItem | MarketplaceComment) -> str:
    if target_type == "post":
        return entity.title or f"post:{entity.id}"
    if target_type == "marketplace_item":
        return entity.title or f"marketplace_item:{entity.id}"
    if target_type == "comment":
        return f"comment:{entity.id}"
    return f"marketplace_comment:{entity.id}"


def _target_preview(target_type: TargetType, entity: Post | Comment | MarketplaceItem | MarketplaceComment) -> str | None:
    if target_type == "post":
        value = entity.content
    elif target_type == "marketplace_item":
        value = entity.description
    else:
        value = entity.content
    return (value or "")[:200] or None


def _target_author(target_type: TargetType, entity: Post | Comment | MarketplaceItem | MarketplaceComment) -> str | None:
    if target_type == "marketplace_item":
        return entity.seller_name
    return entity.author_name


def _target_is_hidden(target_type: TargetType, entity: Post | Comment | MarketplaceItem | MarketplaceComment) -> bool:
    if target_type == "marketplace_item":
        return entity.status == "hidden"
    return bool(entity.is_hidden)


def _target_is_deleted(entity: Post | Comment | MarketplaceItem | MarketplaceComment) -> bool:
    return bool(getattr(entity, "is_deleted", False))


def _impact_scope(target_type: TargetType) -> str:
    return {
        "post": "post_visibility",
        "comment": "comment_visibility",
        "marketplace_item": "marketplace_item_visibility",
        "marketplace_comment": "marketplace_comment_visibility",
    }[target_type]


async def _related_counts(
    db: AsyncSession,
    target_type: TargetType,
    entity: Post | Comment | MarketplaceItem | MarketplaceComment,
) -> dict[str, int]:
    """숨김 자체가 연쇄 삭제하지 않는다는 사실을 UI가 명시하도록 최소 영향 수를 제공한다."""
    if target_type == "post":
        result = await db.execute(
            select(func.count()).select_from(Comment).where(Comment.post_id == entity.id)
        )
        return {"comments": result.scalar() or 0}
    if target_type == "marketplace_item":
        result = await db.execute(
            select(func.count()).select_from(MarketplaceComment).where(MarketplaceComment.item_id == entity.id)
        )
        return {"comments": result.scalar() or 0}
    return {}


def target_state_fingerprint(
    target_type: TargetType,
    entity: Post | Comment | MarketplaceItem | MarketplaceComment,
) -> str:
    """제목·본문(또는 설명)·가시성 상태의 SHA-256 지문을 만든다."""
    snapshot = {
        "target_type": target_type,
        "target_id": str(entity.id),
        "target_name": _display_name(target_type, entity),
        "title": getattr(entity, "title", None),
        "body": getattr(entity, "content", getattr(entity, "description", None)),
        "status": getattr(entity, "status", None),
        "is_hidden": _target_is_hidden(target_type, entity),
        "is_deleted": _target_is_deleted(entity),
    }
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _set_target_hidden(target: ResolvedModerationTarget, is_hidden: bool) -> None:
    """기존 콘텐츠 상태 필드만 사용해 가시성을 변경한다."""
    if target.target_type == "marketplace_item":
        target.entity.status = "hidden" if is_hidden else "active"
    else:
        target.entity.is_hidden = is_hidden
    target.is_hidden = is_hidden


def _assert_action_allowed(target: ResolvedModerationTarget, action: ModerationAction) -> None:
    if target.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="삭제된 댓글은 모더레이션 가시성 변경 대상이 아닙니다.",
        )
    if action == "hide" and target.is_hidden:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="대상이 이미 숨김 상태입니다.",
        )
    if action == "restore" and not target.is_hidden:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="대상이 이미 공개 상태입니다.",
        )


async def resolve_moderation_target(
    db: AsyncSession,
    target_type: TargetType,
    target_id: UUID,
    *,
    for_update: bool = False,
) -> ResolvedModerationTarget:
    """대상을 한 곳에서 해석해 모든 단계가 동일한 상태 판단을 사용하게 한다."""
    if target_type == "post":
        statement = select(Post).where(Post.id == target_id)
    elif target_type == "comment":
        statement = select(Comment).where(Comment.id == target_id)
    elif target_type == "marketplace_item":
        statement = select(MarketplaceItem).where(MarketplaceItem.id == target_id)
    else:
        statement = select(MarketplaceComment).where(MarketplaceComment.id == target_id)

    if for_update:
        statement = statement.with_for_update()

    result = await db.execute(statement)
    entity = result.scalar_one_or_none()
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모더레이션 대상을 찾을 수 없습니다.")

    return ResolvedModerationTarget(
        target_type=target_type,
        target_id=target_id,
        entity=entity,
        target_name=_display_name(target_type, entity),
        target_preview=_target_preview(target_type, entity),
        target_author=_target_author(target_type, entity),
        is_hidden=_target_is_hidden(target_type, entity),
        is_deleted=_target_is_deleted(entity),
        target_fingerprint=target_state_fingerprint(target_type, entity),
        impact_scope=_impact_scope(target_type),
    )


@router.get(
    "/moderation/targets/{target_type}/{target_id}/impact",
    response_model=ModerationTargetImpactResponse,
)
async def get_moderation_target_impact(
    target_type: TargetType,
    target_id: UUID,
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """확인 전 대상의 현재 상태와 다음에 가능한 가시성 변경을 조회한다."""
    target = await resolve_moderation_target(db, target_type, target_id)
    if target.is_deleted:
        available_actions: list[ModerationAction] = []
    else:
        available_actions = ["restore"] if target.is_hidden else ["hide"]
    related_counts = await _related_counts(db, target.target_type, target.entity)
    return ModerationTargetImpactResponse(
        target_type=target.target_type,
        target_id=target.target_id,
        target_name=target.target_name,
        target_preview=target.target_preview,
        target_author=target.target_author,
        is_hidden=target.is_hidden,
        is_deleted=target.is_deleted,
        impact_scope=target.impact_scope,
        related_counts=related_counts,
        available_actions=available_actions,
    )


@router.post(
    "/moderation/confirmations",
    response_model=ModerationConfirmationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def issue_moderation_confirmation(
    body: ModerationConfirmationRequest,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """정확한 대상명과 사유를 확인한 뒤 10분짜리 일회용 토큰을 발급한다."""
    target = await resolve_moderation_target(db, body.target_type, body.target_id)
    if not _constant_time_name_match(body.target_name, target.target_name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="입력한 대상명이 현재 대상명과 일치하지 않습니다.",
        )
    _assert_action_allowed(target, body.action)

    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    expires_at = datetime.now(timezone.utc) + CONFIRMATION_TOKEN_TTL
    confirmation = ModerationConfirmation(
        token_digest=confirmation_token_digest(raw_token),
        target_type=body.target_type,
        target_id=body.target_id,
        target_name=target.target_name,
        target_fingerprint=target.target_fingerprint,
        action=body.action,
        reason=body.reason,
        requested_by=actor_id,
        expires_at=expires_at,
    )
    db.add(confirmation)
    await db.commit()

    return ModerationConfirmationResponse(
        confirmation_token=raw_token,
        expires_at=expires_at,
        target_type=body.target_type,
        target_id=body.target_id,
        target_name=target.target_name,
        impact_scope=target.impact_scope,
        action=body.action,
    )


@router.post("/moderation/actions", response_model=ModerationActionResponse)
async def apply_moderation_action(
    body: ModerationActionRequest,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    actor_role: str = Depends(require_role("admin", "moderator")),
):
    """잠금·만료·소유자 검증 뒤 토큰을 한 번 소비해 숨김 또는 복구를 실행한다."""
    token_digest = confirmation_token_digest(body.confirmation_token)
    now = datetime.now(timezone.utc)

    async with db.begin():
        result = await db.execute(
            select(ModerationConfirmation)
            .where(ModerationConfirmation.token_digest == token_digest)
            .with_for_update()
        )
        confirmation = result.scalar_one_or_none()
        if confirmation is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효하지 않은 확인 토큰입니다.")
        if confirmation.consumed_at is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 사용된 확인 토큰입니다.")
        if confirmation_is_expired(confirmation.expires_at, now):
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="확인 토큰이 만료되었습니다.")
        if confirmation.requested_by != actor_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="다른 관리자가 발급한 확인 토큰입니다.")
        if (
            confirmation.target_type != body.target_type
            or confirmation.target_id != body.target_id
            or confirmation.action != body.action
        ):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="확인 토큰과 실행 요청이 일치하지 않습니다.")

        target = await resolve_moderation_target(
            db,
            body.target_type,
            body.target_id,
            for_update=True,
        )
        if not _constant_time_name_match(confirmation.target_name, target.target_name):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="대상명이 변경되어 새 확인이 필요합니다.",
            )
        if not hmac.compare_digest(confirmation.target_fingerprint, target.target_fingerprint):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="대상 내용 또는 상태가 변경되어 새 확인이 필요합니다.",
            )
        _assert_action_allowed(target, body.action)

        prior_hidden = target.is_hidden
        _set_target_hidden(target, body.action == "hide")
        confirmation.consumed_at = now
        confirmation.consumed_by = actor_id
        event = ModerationEvent(
            confirmation_id=confirmation.id,
            target_type=target.target_type,
            target_id=target.target_id,
            target_name=target.target_name,
            impact_scope=target.impact_scope,
            action=body.action,
            reason=confirmation.reason,
            actor_id=actor_id,
            actor_role=actor_role,
            prior_hidden=prior_hidden,
            resulting_hidden=target.is_hidden,
        )
        db.add(event)
        await db.flush()

    return ModerationActionResponse(
        event_id=event.id,
        target_type=target.target_type,
        target_id=target.target_id,
        action=body.action,
        is_hidden=target.is_hidden,
        created_at=event.created_at,
    )


@router.get("/moderation/events", response_model=ModerationEventListResponse)
async def list_moderation_events(
    target_type: TargetType | None = Query(None),
    target_id: UUID | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """변경 불가능한 감사 이벤트를 대상 기준으로 추적한다."""
    filters = []
    if target_type is not None:
        filters.append(ModerationEvent.target_type == target_type)
    if target_id is not None:
        filters.append(ModerationEvent.target_id == target_id)

    count_statement = select(func.count()).select_from(ModerationEvent)
    for condition in filters:
        count_statement = count_statement.where(condition)
    total_result = await db.execute(count_statement)
    total = total_result.scalar() or 0

    statement = select(ModerationEvent).order_by(ModerationEvent.created_at.desc())
    for condition in filters:
        statement = statement.where(condition)
    statement = statement.offset((page - 1) * size).limit(size)
    result = await db.execute(statement)
    events = result.scalars().all()

    return ModerationEventListResponse(
        items=[ModerationEventResponse.model_validate(event) for event in events],
        total=total,
        page=page,
        size=size,
    )
