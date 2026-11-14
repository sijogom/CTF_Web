"""플랫폼 이벤트 공개·관리 API.

CTF 대회와 커뮤니티 공지는 이 라우터의 입력·출력 또는 저장소에 포함하지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_current_user_id, require_role
from app.database import get_db
from app.models.event import EventStatus, PlatformEvent, PlatformEventAuditEvent, PlatformEventCategory, PlatformEventCategoryAuditEvent
from app.schemas.event import (
    EventAdminDetail,
    EventAdminListResponse,
    EventArchiveRequest,
    EventAuditEventResponse,
    EventAuditListResponse,
    EventCreate,
    EventListResponse,
    EventPublicDetail,
    EventRestoreRequest,
    EventStatusValue,
    EventSummary,
    EventUpdate,
    EventCategory,
    EventCategoryCreate,
    EventCategoryUpdate,
    EventCategoryResponse,
    EventCategoryListResponse,
)
from app.utils.event_visibility import event_phase, is_publicly_visible, validate_event_schedule
from app.utils.safe_html import RichContentError, normalize_rich_content_for_write


router = APIRouter(tags=["events"])
_EVENT_MANAGER_ROLES = ("admin", "moderator")
_NON_NULLABLE_UPDATE_FIELDS = {
    "title",
    "content_markdown",
    "starts_at",
    "ends_at",
    "visible_from_at",
    "category",
    "status",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _event_audit_state(event: PlatformEvent) -> dict:
    """본문·요청 인증정보를 복제하지 않는 최소 감사 스냅샷."""
    return {
        "status": event.status,
        "category": event.category,
        "starts_at": event.starts_at.isoformat(),
        "ends_at": event.ends_at.isoformat(),
        "visible_from_at": event.visible_from_at.isoformat(),
        "visible_until_at": event.visible_until_at.isoformat() if event.visible_until_at else None,
        "has_location": bool(event.location),
        "has_external_url": bool(event.external_url),
    }


def _add_event_audit(
    db: AsyncSession,
    *,
    event: PlatformEvent,
    actor_id: UUID,
    action: str,
    details: dict,
) -> None:
    db.add(
        PlatformEventAuditEvent(
            event_id=event.id,
            actor_id=actor_id,
            action=action,
            details=details,
        )
    )


def _event_summary(event: PlatformEvent, *, now: datetime) -> EventSummary:
    return EventSummary(
        id=event.id,
        title=event.title,
        summary=event.summary,
        category=event.category,
        category_labels=event.category_definition.labels,
        location=event.location,
        external_url=event.external_url,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        event_phase=event_phase(starts_at=event.starts_at, ends_at=event.ends_at, now=now),
    )


def _public_detail(event: PlatformEvent, *, now: datetime) -> EventPublicDetail:
    return EventPublicDetail(
        **_event_summary(event, now=now).model_dump(),
        content_markdown=event.content_markdown,
        visible_from_at=event.visible_from_at,
        visible_until_at=event.visible_until_at,
    )


def _admin_detail(event: PlatformEvent, *, now: datetime) -> EventAdminDetail:
    return EventAdminDetail(
        **_public_detail(event, now=now).model_dump(),
        status=event.status,
        published_at=event.published_at,
        archived_at=event.archived_at,
        archive_reason=event.archive_reason,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


async def _event_or_404(
    db: AsyncSession,
    event_id: UUID,
    *,
    lock: bool = False,
) -> PlatformEvent:
    statement = select(PlatformEvent).where(PlatformEvent.id == event_id)
    if lock:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    event = result.scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="이벤트를 찾을 수 없습니다.")
    return event


def _ensure_update_is_valid(event: PlatformEvent, updates: dict) -> None:
    for field_name in _NON_NULLABLE_UPDATE_FIELDS:
        if field_name in updates and updates[field_name] is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{field_name}은(는) null로 변경할 수 없습니다.",
            )

    try:
        validate_event_schedule(
            starts_at=updates.get("starts_at", event.starts_at),
            ends_at=updates.get("ends_at", event.ends_at),
            visible_from_at=updates.get("visible_from_at", event.visible_from_at),
            visible_until_at=updates.get("visible_until_at", event.visible_until_at),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


def _public_visibility_conditions(now: datetime):
    return (
        PlatformEvent.status == EventStatus.PUBLISHED.value,
        PlatformEvent.visible_from_at <= now,
        or_(
            PlatformEvent.visible_until_at.is_(None),
            PlatformEvent.visible_until_at > now,
        ),
    )


async def _category_or_error(db: AsyncSession, key: str, *, lock: bool = False, status_code: int = 422) -> PlatformEventCategory:
    statement = select(PlatformEventCategory).where(PlatformEventCategory.key == key)
    if lock:
        statement = statement.with_for_update()
    category = (await db.execute(statement)).scalar_one_or_none()
    if category is None:
        raise HTTPException(status_code=status_code, detail="등록되지 않은 이벤트 카테고리입니다.")
    return category


def _category_response(category: PlatformEventCategory) -> EventCategoryResponse:
    return EventCategoryResponse(key=category.key, labels=category.labels, sort_order=category.sort_order, revision=category.revision)


async def _category_list(db: AsyncSession) -> EventCategoryListResponse:
    rows = (await db.execute(select(PlatformEventCategory).order_by(PlatformEventCategory.sort_order, PlatformEventCategory.key))).scalars().all()
    return EventCategoryListResponse(items=[_category_response(item) for item in rows])


async def _category_operation(db: AsyncSession, operation: tuple[UUID, UUID, str]) -> PlatformEventCategory | None:
    operation_id, actor_id, request_hash = operation
    category = (await db.execute(select(PlatformEventCategory).where(PlatformEventCategory.operation_id == operation_id))).scalar_one_or_none()
    if category is not None and (category.created_by != actor_id or category.create_request_hash != request_hash):
        raise HTTPException(status_code=409, detail={"code": "event_category_operation_conflict", "message": "같은 등록 요청을 다른 내용으로 재사용할 수 없습니다."})
    return category


async def _persist_category(db: AsyncSession, *, flush: bool = False, replay_operation: tuple[UUID, UUID, str] | None = None) -> PlatformEventCategory | None:
    try:
        await (db.flush() if flush else db.commit())
    except IntegrityError as exc:
        await db.rollback()
        if replay_operation is not None:
            category = await _category_operation(db, replay_operation)
            if category is not None:
                return category
        if "uq_platform_event_categories_ko_label" in str(exc.orig):
            raise HTTPException(status_code=409, detail={"code": "event_category_duplicate", "message": "같은 한국어 이름의 이벤트 카테고리가 있습니다."}) from exc
        raise


@router.get("/event-categories", response_model=EventCategoryListResponse)
async def list_public_event_categories(db: AsyncSession = Depends(get_db)):
    return await _category_list(db)


@router.get("/admin/event-categories", response_model=EventCategoryListResponse)
async def list_admin_event_categories(db: AsyncSession = Depends(get_db), _role: str = Depends(require_role(*_EVENT_MANAGER_ROLES))):
    return await _category_list(db)


@router.post("/admin/event-categories", response_model=EventCategoryResponse, status_code=201)
async def create_event_category(
    body: EventCategoryCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role(*_EVENT_MANAGER_ROLES)),
):
    labels = body.labels.model_dump()
    request_hash = hashlib.sha256(json.dumps({"labels": labels, "sort_order": body.sort_order}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    operation_id = body.operation_id or uuid4()
    operation = (operation_id, actor_id, request_hash)
    existing = await _category_operation(db, operation)
    if existing is not None:
        return _category_response(existing)
    category = PlatformEventCategory(key="c_" + uuid4().hex[:28], labels=labels, sort_order=body.sort_order, revision=1, operation_id=operation_id, create_request_hash=request_hash, created_by=actor_id, updated_by=actor_id)
    db.add(category)
    # Flush uniqueness before adding a dependent audit row; both remain in this transaction.
    replayed = await _persist_category(db, flush=True, replay_operation=operation)
    if replayed is not None:
        return _category_response(replayed)
    db.add(PlatformEventCategoryAuditEvent(category_key=category.key, actor_id=actor_id, action="event_category.create", details={"after": _category_response(category).model_dump()}))
    await _persist_category(db)
    return _category_response(category)


@router.patch("/admin/event-categories/{category_key}", response_model=EventCategoryResponse)
async def update_event_category(
    category_key: EventCategory,
    body: EventCategoryUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role(*_EVENT_MANAGER_ROLES)),
):
    category = await _category_or_error(db, category_key, lock=True, status_code=404)
    if category.revision != body.expected_revision:
        raise HTTPException(status_code=409, detail={"code": "event_category_revision_conflict", "message": "다른 관리자가 카테고리를 수정했습니다. 최신 목록을 다시 확인해 주세요."})
    before = _category_response(category).model_dump()
    category.labels = body.labels.model_dump()
    category.sort_order = body.sort_order
    category.revision += 1
    category.updated_by = actor_id
    category.updated_at = _utc_now()
    db.add(PlatformEventCategoryAuditEvent(category_key=category.key, actor_id=actor_id, action="event_category.update", details={"before": before, "after": _category_response(category).model_dump()}))
    await _persist_category(db)
    return _category_response(category)


@router.get("/events", response_model=EventListResponse)
async def list_public_events(
    category: EventCategory | None = None,
    include_ended: bool = False,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """서버 공개 조건을 만족하는 이벤트만 반환한다."""
    now = _utc_now()
    conditions = list(_public_visibility_conditions(now))
    if category:
        await _category_or_error(db, category)
        conditions.append(PlatformEvent.category == category)
    if not include_ended:
        conditions.append(PlatformEvent.ends_at > now)

    total = int(
        (
            await db.execute(
                select(func.count()).select_from(PlatformEvent).where(*conditions)
            )
        ).scalar_one()
    )
    result = await db.execute(
        select(PlatformEvent)
        .where(*conditions)
        .order_by(
            PlatformEvent.starts_at.asc(),
            PlatformEvent.created_at.desc(),
            PlatformEvent.id.desc(),
        )
        .offset((page - 1) * size)
        .limit(size)
    )
    return EventListResponse(
        items=[_event_summary(item, now=now) for item in result.scalars().all()],
        total=total,
        page=page,
        size=size,
    )


@router.get("/events/{event_id}", response_model=EventPublicDetail)
async def get_public_event(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """공개 상태가 아닌 이벤트의 존재 여부를 노출하지 않는다."""
    now = _utc_now()
    event = await _event_or_404(db, event_id)
    if not is_publicly_visible(
        status=event.status,
        visible_from_at=event.visible_from_at,
        visible_until_at=event.visible_until_at,
        now=now,
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="이벤트를 찾을 수 없습니다.")
    return _public_detail(event, now=now)


@router.get("/admin/events", response_model=EventAdminListResponse)
async def list_admin_events(
    event_status: EventStatusValue | None = Query(None, alias="status"),
    category: EventCategory | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role(*_EVENT_MANAGER_ROLES)),
):
    """관리자·모더레이터용 전체 이벤트 목록."""
    conditions = []
    if event_status:
        conditions.append(PlatformEvent.status == event_status)
    if category:
        await _category_or_error(db, category)
        conditions.append(PlatformEvent.category == category)

    total = int(
        (
            await db.execute(
                select(func.count()).select_from(PlatformEvent).where(*conditions)
            )
        ).scalar_one()
    )
    result = await db.execute(
        select(PlatformEvent)
        .where(*conditions)
        .order_by(
            PlatformEvent.starts_at.desc(),
            PlatformEvent.created_at.desc(),
            PlatformEvent.id.desc(),
        )
        .offset((page - 1) * size)
        .limit(size)
    )
    now = _utc_now()
    return EventAdminListResponse(
        items=[_admin_detail(item, now=now) for item in result.scalars().all()],
        total=total,
        page=page,
        size=size,
    )


@router.get("/admin/events/{event_id}", response_model=EventAdminDetail)
async def get_admin_event(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role(*_EVENT_MANAGER_ROLES)),
):
    return _admin_detail(await _event_or_404(db, event_id), now=_utc_now())


@router.get("/admin/events/{event_id}/audit-events", response_model=EventAuditListResponse)
async def list_event_audit_events(
    event_id: UUID,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role(*_EVENT_MANAGER_ROLES)),
):
    """본문 원문을 포함하지 않는 이벤트 감사 이력을 반환한다."""
    await _event_or_404(db, event_id)
    total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(PlatformEventAuditEvent)
                .where(PlatformEventAuditEvent.event_id == event_id)
            )
        ).scalar_one()
    )
    result = await db.execute(
        select(PlatformEventAuditEvent)
        .where(PlatformEventAuditEvent.event_id == event_id)
        .order_by(
            PlatformEventAuditEvent.created_at.desc(),
            PlatformEventAuditEvent.id.desc(),
        )
        .offset((page - 1) * size)
        .limit(size)
    )
    return EventAuditListResponse(
        items=[
            EventAuditEventResponse(
                id=item.id,
                actor_id=item.actor_id,
                action=item.action,
                details=item.details,
                created_at=item.created_at,
            )
            for item in result.scalars().all()
        ],
        total=total,
        page=page,
        size=size,
    )


@router.post("/admin/events", response_model=EventAdminDetail, status_code=status.HTTP_201_CREATED)
async def create_event(
    body: EventCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role(*_EVENT_MANAGER_ROLES)),
):
    now = _utc_now()
    category_definition = await _category_or_error(db, body.category)
    event = PlatformEvent(
        **body.model_dump(),
        created_by=actor_id,
        updated_by=actor_id,
        published_at=now if body.status == EventStatus.PUBLISHED.value else None,
        category_definition=category_definition,
    )
    db.add(event)
    await db.flush()
    _add_event_audit(
        db,
        event=event,
        actor_id=actor_id,
        action="event.create",
        details={"after": _event_audit_state(event)},
    )
    await db.commit()
    await db.refresh(event)
    return _admin_detail(event, now=now)


@router.patch("/admin/events/{event_id}", response_model=EventAdminDetail)
async def update_event(
    event_id: UUID,
    body: EventUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role(*_EVENT_MANAGER_ROLES)),
):
    event = await _event_or_404(db, event_id, lock=True)
    if event.status == EventStatus.ARCHIVED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="보관된 이벤트는 복구한 뒤 수정할 수 있습니다.",
        )

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="수정할 값이 없습니다.")
    if "content_markdown" not in updates:
        try:
            updates["content_markdown"] = normalize_rich_content_for_write(
                event.content_markdown,
                field_name="본문",
                max_visible_chars=12000,
            )
        except RichContentError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=error.as_detail(),
            ) from error
    _ensure_update_is_valid(event, updates)
    if "category" in updates:
        event.category_definition = await _category_or_error(db, updates["category"])

    before = _event_audit_state(event)
    prior_status = event.status
    now = _utc_now()
    for field_name, value in updates.items():
        setattr(event, field_name, value)
    if event.status == EventStatus.PUBLISHED.value and prior_status != EventStatus.PUBLISHED.value:
        event.published_at = event.published_at or now
    event.updated_by = actor_id
    event.updated_at = now
    _add_event_audit(
        db,
        event=event,
        actor_id=actor_id,
        action="event.update",
        details={
            "before": before,
            "after": _event_audit_state(event),
            # 본문·URL·장소 원문을 복제하지 않으면서도 어떤 관리 필드가 바뀌었는지
            # 감사자가 판별할 수 있게 한다.
            "changed_fields": sorted(updates),
        },
    )
    await db.commit()
    await db.refresh(event)
    return _admin_detail(event, now=now)


@router.post("/admin/events/{event_id}/archive", response_model=EventAdminDetail)
async def archive_event(
    event_id: UUID,
    body: EventArchiveRequest,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role(*_EVENT_MANAGER_ROLES)),
):
    event = await _event_or_404(db, event_id, lock=True)
    if event.status == EventStatus.ARCHIVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 보관된 이벤트입니다.")

    now = _utc_now()
    before = _event_audit_state(event)
    event.status = EventStatus.ARCHIVED.value
    event.archived_at = now
    event.archived_by = actor_id
    event.archive_reason = body.reason
    event.updated_by = actor_id
    event.updated_at = now
    _add_event_audit(
        db,
        event=event,
        actor_id=actor_id,
        action="event.archive",
        details={"before": before, "after": _event_audit_state(event), "reason_recorded": True},
    )
    await db.commit()
    await db.refresh(event)
    return _admin_detail(event, now=now)


@router.post("/admin/events/{event_id}/restore", response_model=EventAdminDetail)
async def restore_event(
    event_id: UUID,
    body: EventRestoreRequest,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role(*_EVENT_MANAGER_ROLES)),
):
    event = await _event_or_404(db, event_id, lock=True)
    if event.status != EventStatus.ARCHIVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="보관된 이벤트만 복구할 수 있습니다.")

    now = _utc_now()
    before = _event_audit_state(event)
    event.status = body.status
    if event.status == EventStatus.PUBLISHED.value:
        event.published_at = event.published_at or now
    event.archived_at = None
    event.archived_by = None
    event.archive_reason = None
    event.updated_by = actor_id
    event.updated_at = now
    _add_event_audit(
        db,
        event=event,
        actor_id=actor_id,
        action="event.restore",
        details={"before": before, "after": _event_audit_state(event)},
    )
    await db.commit()
    await db.refresh(event)
    return _admin_detail(event, now=now)
