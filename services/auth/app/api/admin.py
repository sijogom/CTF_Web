import logging
from datetime import datetime, timezone
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, BeforeValidator, Field, field_validator
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.competition import Competition
from app.api.deps import require_role
from app.utils.audit import record_audit
from app.utils.admin_settings import (
    ADMIN_SETTINGS_UNAVAILABLE_DETAIL,
    SMTP_SETTINGS_INVALID_DETAIL,
    SMTP_SETTINGS_KEYS,
    AdminSettingsStorageError,
    InvalidSmtpSettings,
    load_admin_settings,
    prepare_smtp_settings_update,
    refresh_admin_settings_cache,
    save_admin_settings,
)
from app.utils.email_verification import clear_verification_artifacts, issue_verification_code

logger = logging.getLogger(__name__)
router = APIRouter(tags=["관리자"])


def _parse_audit_query_datetime(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("시간대가 포함된 ISO 8601 시각이 필요합니다.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("시간대가 포함된 ISO 8601 시각이 필요합니다.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("시간대가 포함된 ISO 8601 시각이 필요합니다.")
    return value


AuditQueryDateTime = Annotated[datetime, BeforeValidator(_parse_audit_query_datetime)]


def _escape_like(value: str) -> str:
    """LIKE 패턴의 와일드카드 문자를 이스케이프한다."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

LEARN_URL = settings.learn_service_url
ENV_ORCH_URL = settings.env_orchestrator_url


def _mask_smtp_password(data: dict) -> dict:
    """응답에서 smtp_password를 마스킹하여 반환한다."""
    result = {**data}
    if result.get("smtp_password"):
        result["smtp_password"] = "********"
    return result


@router.get("/stats")
async def admin_stats(
    user: User = Depends(require_role("admin", "moderator")),
    db: AsyncSession = Depends(get_db),
):
    """대시보드 통계 — 사용자/대회는 자체 DB, 문제 수는 learn-service에서 조회."""
    total_users = (await db.execute(select(func.count(User.id)))).scalar() or 0
    active_users = (
        await db.execute(select(func.count(User.id)).where(User.is_active == True))  # noqa: E712
    ).scalar() or 0

    # 진행 중 대회 수 (자체 DB)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    active_contests = (
        await db.execute(
            select(func.count()).select_from(Competition).where(
                Competition.start_at <= now,
                Competition.end_at > now,
                Competition.is_deleted.is_(False),
            )
        )
    ).scalar() or 0

    # learn-service에서 문제/풀이 통계 조회
    total_challenges = 0
    internal_headers = {"X-Internal-Key": settings.internal_api_key} if settings.internal_api_key else {}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{LEARN_URL}/internal/stats", headers=internal_headers)
            if resp.status_code == 200:
                learn_stats = resp.json()
                total_challenges = learn_stats.get("total_challenges", 0)
    except Exception as exc:
        logger.warning("learn-service 통계 조회 실패: %s", exc)

    # env-orchestrator에서 활성 컨테이너 수 조회 (active_instances = 실제 Redis 인스턴스 수)
    active_containers = 0
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{ENV_ORCH_URL}/health")
            if resp.status_code == 200:
                health = resp.json()
                active_containers = health.get("active_instances", health.get("allocated_ports", 0))
    except Exception as exc:
        logger.warning("env-orchestrator 통계 조회 실패: %s", exc)

    return {
        "total_users": total_users,
        "active_users": active_users,
        "total_challenges": total_challenges,
        "active_contests": active_contests,
        "active_containers": active_containers,
    }


@router.get("/users")
async def admin_users(
    user: User = Depends(require_role("admin", "moderator")),
    db: AsyncSession = Depends(get_db),
    search: str = Query("", description="검색어 (사용자명 또는 이메일)"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    """유저 목록 (검색, 페이지네이션)"""
    query = select(User)

    if search:
        escaped = _escape_like(search)
        query = query.where(
            User.username.ilike(f"%{escaped}%", escape="\\")
            | User.email.ilike(f"%{escaped}%", escape="\\")
        )

    # 전체 수
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # 페이지네이션
    query = query.order_by(User.created_at.desc(), User.id.desc()).offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    users = result.scalars().all()

    return {
        "total": total,
        "page": page,
        "size": size,
        "items": [
            {
                "id": str(u.id),
                "username": u.username,
                "email": u.email,
                "role": u.role,
                "is_active": u.is_active,
                "email_verified": u.email_verified,
                "tier_override": u.tier_override,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ],
    }


@router.patch("/users/{user_id}/role")
async def change_user_role(
    user_id: str,
    request: Request,
    role: str = Query(..., description="변경할 역할 (user, moderator, admin)"),
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """유저 역할 변경 — admin만 가능. moderator는 콘텐츠 관리만 가능하며 역할 변경 권한 없음."""
    valid_roles = ("user", "moderator", "admin")
    if role not in valid_roles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"유효하지 않은 역할입니다. 허용: {', '.join(valid_roles)}",
        )

    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="유저를 찾을 수 없습니다.")

    if str(target_user.id) == str(admin.id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="자신의 역할은 변경할 수 없습니다.")

    # admin 역할 부여 방지 — 최고 관리자는 DB 직접 설정으로만 추가
    if role == "admin" and admin.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin 역할은 최고 관리자만 부여할 수 있습니다.")

    old_role = target_user.role
    target_user.role = role
    await record_audit(
        db,
        actor_id=str(admin.id),
        action="user.role_change",
        target_type="user",
        target_id=str(target_user.id),
        details={"username": target_user.username, "old_role": old_role, "new_role": role},
        request=request,
    )
    await db.commit()

    # DB의 현재 역할이 최종 권한 기준이다. Redis가 일시적으로 불가해도
    # 역할 변경 자체를 롤백하면 기존 권한이 유지되므로, 커밋 뒤 refresh
    # token 폐기를 시도하고 실패는 명시적으로 관리자에게 알린다.
    refresh_revocation_pending = False
    try:
        from app.api.refresh import _revoke_all_user_tokens
        await _revoke_all_user_tokens(str(target_user.id))
    except Exception:
        refresh_revocation_pending = True
        logger.exception("역할 변경 후 refresh token 폐기 실패 (user_id=%s)", target_user.id)

    return {
        "message": f"유저 {target_user.username}의 역할이 {role}(으)로 변경되었습니다.",
        "refresh_revocation_pending": refresh_revocation_pending,
    }


@router.patch("/users/{user_id}/tier")
async def change_user_tier(
    user_id: str,
    request: Request,
    tier: str = Query("", description="티어 이름 (빈 문자열이면 자동 모드)"),
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """유저 티어 강제 설정 — admin만 가능. 빈 문자열이면 XP 기반 자동 모드로 복귀."""
    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail="유저를 찾을 수 없습니다.")

    old_tier = target_user.tier_override
    target_user.tier_override = tier if tier else None
    await record_audit(
        db,
        actor_id=str(admin.id),
        action="user.tier_change",
        target_type="user",
        target_id=str(target_user.id),
        details={"username": target_user.username, "old_tier": old_tier, "new_tier": tier or "auto"},
        request=request,
    )
    await db.commit()
    mode = f"'{tier}' 강제 설정" if tier else "자동(XP 기반)"
    return {"message": f"유저 {target_user.username}의 티어가 {mode}(으)로 변경되었습니다."}


@router.patch("/users/{user_id}/active")
async def toggle_user_active(
    user_id: str,
    request: Request,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """유저 활성/비활성화 (admin only)"""
    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="유저를 찾을 수 없습니다.")

    if str(target_user.id) == str(admin.id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="자신의 계정은 비활성화할 수 없습니다.")

    target_user.is_active = not target_user.is_active
    new_status = "활성화" if target_user.is_active else "비활성화"
    await record_audit(
        db,
        actor_id=str(admin.id),
        action="user.toggle_active",
        target_type="user",
        target_id=str(target_user.id),
        details={"username": target_user.username, "is_active": target_user.is_active},
        request=request,
    )
    await db.commit()

    refresh_revocation_pending = False
    if not target_user.is_active:
        # 비활성화의 즉시 권한 차단은 DB 상태와 하위 서비스의 현재 권한 조회가
        # 담당한다. Redis 폐기 실패가 비활성화를 되돌리지는 않도록 분리한다.
        try:
            from app.api.refresh import _revoke_all_user_tokens
            await _revoke_all_user_tokens(str(target_user.id))
        except Exception:
            refresh_revocation_pending = True
            logger.exception("비활성화 후 refresh token 폐기 실패 (user_id=%s)", target_user.id)

    return {
        "message": f"유저 {target_user.username}이(가) {new_status}되었습니다.",
        "is_active": target_user.is_active,
        "refresh_revocation_pending": refresh_revocation_pending,
    }


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    request: Request,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """유저 계정 영구 삭제 — admin만 가능."""
    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="유저를 찾을 수 없습니다.")

    if str(target_user.id) == str(admin.id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="자신의 계정은 삭제할 수 없습니다.")

    username = target_user.username
    target_id = str(target_user.id)
    await record_audit(
        db,
        actor_id=str(admin.id),
        action="user.delete",
        target_type="user",
        target_id=target_id,
        details={"username": username, "email": target_user.email},
        request=request,
    )
    await db.delete(target_user)
    await db.commit()

    return {"message": f"유저 '{username}'의 계정이 영구 삭제되었습니다."}


@router.get("/audit-logs")
async def admin_audit_logs(
    user: User = Depends(require_role("admin", "moderator")),
    action: str = Query("", description="액션 필터"),
    action_prefix: str = Query("", max_length=100, description="액션 접두사 필터"),
    search: str = Query("", max_length=200, description="액션·사용자·대상·IP 검색"),
    created_from: AuditQueryDateTime | None = Query(None, description="시간대 포함 ISO 8601 시작 시각"),
    created_to: AuditQueryDateTime | None = Query(None, description="시간대 포함 ISO 8601 종료 시각"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """감사 로그 조회 — admin 및 moderator만 접근 가능"""
    from app.models.audit import AuditLog
    from sqlalchemy import desc

    def normalize_time(value: datetime | None, field_name: str) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{field_name}에는 시간대가 포함된 ISO 8601 시간이 필요합니다.",
            )
        return value.astimezone(timezone.utc)

    created_from = normalize_time(created_from, "created_from")
    created_to = normalize_time(created_to, "created_to")
    if created_from and created_to and created_from > created_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="created_from은 created_to보다 늦을 수 없습니다.",
        )

    query = select(AuditLog)
    conditions = []
    if action:
        conditions.append(AuditLog.action == action)
    if action_prefix:
        escaped_prefix = _escape_like(action_prefix.strip())
        if escaped_prefix:
            conditions.append(AuditLog.action.like(f"{escaped_prefix}%", escape="\\"))
    if created_from is not None:
        conditions.append(AuditLog.created_at >= created_from)
    if created_to is not None:
        conditions.append(AuditLog.created_at <= created_to)
    normalized_search = search.strip()
    if normalized_search:
        escaped_search = _escape_like(normalized_search)
        pattern = f"%{escaped_search}%"
        matching_actor_ids = select(User.id).where(User.username.ilike(pattern, escape="\\"))
        matching_target_ids = select(cast(User.id, String)).where(User.username.ilike(pattern, escape="\\"))
        conditions.append(
            or_(
                AuditLog.action.ilike(pattern, escape="\\"),
                AuditLog.target_type.ilike(pattern, escape="\\"),
                AuditLog.target_id.ilike(pattern, escape="\\"),
                AuditLog.ip_address.ilike(pattern, escape="\\"),
                AuditLog.actor_id.in_(matching_actor_ids),
                AuditLog.target_id.in_(matching_target_ids),
            )
        )
    if conditions:
        query = query.where(*conditions)

    # 전체 건수
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # 페이지네이션 (최신순)
    query = query.order_by(desc(AuditLog.created_at), desc(AuditLog.id)).offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    logs = result.scalars().all()

    # actor + target user 이름 일괄 조회
    user_ids = set()
    for log in logs:
        if log.actor_id:
            user_ids.add(log.actor_id)
        if log.target_type == "user" and log.target_id:
            user_ids.add(log.target_id)
    user_names: dict[str, str] = {}
    if user_ids:
        name_result = await db.execute(
            select(User.id, User.username).where(User.id.in_(user_ids))
        )
        user_names = {str(row.id): row.username for row in name_result.all()}

    def _target_display(log_entry) -> str:
        """대상 표시: user면 username, 그 외면 type:id 또는 빈값 처리."""
        if not log_entry.target_type:
            return ""
        if log_entry.target_type == "user" and log_entry.target_id:
            name = user_names.get(str(log_entry.target_id))
            return name or str(log_entry.target_id)
        if log_entry.target_id:
            return f"{log_entry.target_type}:{log_entry.target_id}"
        return log_entry.target_type

    return {
        "total": total,
        "page": page,
        "size": size,
        "items": [
            {
                "id": str(log.id),
                "actor": user_names.get(str(log.actor_id), "system") if log.actor_id else "system",
                "action": log.action,
                "target": _target_display(log),
                "details": log.details,
                "ip_address": log.ip_address,
                "created_at": log.created_at.isoformat(),
            }
            for log in logs
        ],
    }


class AdminSettingsRequest(BaseModel):
    site_name: str | None = Field(default=None, min_length=1, max_length=100)
    registration_open: bool | None = None
    max_containers: int | None = Field(default=None, ge=1, le=100_000)
    container_timeout: int | None = Field(default=None, ge=1, le=1_440)
    # SMTP 설정
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_from: str | None = None
    smtp_tls: bool | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    # 티어 설정 (JSON 배열)
    tiers: list[dict] | None = None

    @field_validator("site_name")
    @classmethod
    def normalize_site_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("사이트 이름을 입력해 주세요.")
        return normalized


@router.patch("/settings")
async def update_admin_settings(
    req: AdminSettingsRequest,
    request: Request,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """관리자 설정 업데이트 — PostgreSQL과 감사 로그를 먼저 커밋한다."""

    updates = req.model_dump(exclude_none=True)
    smtp_input = {key: updates.pop(key) for key in SMTP_SETTINGS_KEYS if key in updates}
    try:
        smtp_updates = prepare_smtp_settings_update(smtp_input)
    except InvalidSmtpSettings as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=SMTP_SETTINGS_INVALID_DETAIL,
        ) from exc

    if not updates and not smtp_updates:
        return {"message": "변경할 설정이 없습니다."}

    try:
        old_settings, _ = await load_admin_settings(db)
    except AdminSettingsStorageError as exc:
        logger.warning("관리자 설정 조회 실패: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ADMIN_SETTINGS_UNAVAILABLE_DETAIL,
        ) from exc

    current = {**old_settings, **updates, **smtp_updates}
    changed_keys = [*updates.keys(), *smtp_updates.keys()]

    # DB 설정과 감사 기록을 하나의 커밋으로 남긴다. Redis 갱신은 커밋 이후에만 한다.
    try:
        await save_admin_settings(db, current)

        # SMTP 연결 정보·비밀번호는 감사 세부값에 남기지 않고 변경 키만 남긴다.
        await record_audit(
            db,
            actor_id=str(user.id),
            action="settings.smtp_update" if not updates else "settings.update",
            target_type="settings",
            details={"changed_keys": changed_keys, "new_values": updates},
            request=request,
        )
        await db.commit()
    except AdminSettingsStorageError as exc:
        await db.rollback()
        logger.warning("관리자 설정 저장 실패: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ADMIN_SETTINGS_UNAVAILABLE_DETAIL,
        ) from exc
    except Exception:
        await db.rollback()
        logger.exception("관리자 설정 저장 또는 감사 기록 실패")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="설정을 저장하지 못했습니다.")

    cache_refreshed = await refresh_admin_settings_cache(current)
    return {
        "message": "설정이 저장되었습니다." if cache_refreshed else "설정은 저장되었으나 캐시 갱신이 보류되었습니다.",
        "settings": _mask_smtp_password(current),
        "cache_refresh_pending": not cache_refreshed,
    }


@router.get("/settings")
async def get_admin_settings(
    user: User = Depends(require_role("admin", "moderator")),
    db: AsyncSession = Depends(get_db),
):
    """PostgreSQL 기준 관리자 설정을 조회하고 Redis 캐시를 최선으로 복구한다."""
    try:
        current, source = await load_admin_settings(db)
    except AdminSettingsStorageError as exc:
        logger.warning("관리자 설정 조회 실패: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ADMIN_SETTINGS_UNAVAILABLE_DETAIL,
        ) from exc
    response = _mask_smtp_password(current)
    response["source"] = source
    response["cache_refresh_pending"] = source.endswith("_cache_unavailable")
    return response


class TestEmailRequest(BaseModel):
    to: str | None = None


@router.post("/test-email")
async def send_test_email(
    req: TestEmailRequest,
    request: Request,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """SMTP 테스트 메일 발송 — admin만 가능."""
    from app.email.sender import mask_email, send_email
    from app.email.templates import test_email_template

    recipient = req.to or user.email
    try:
        subject, html_body = test_email_template()
        await send_email(recipient, subject, html_body)
    except Exception as exc:
        logger.warning("테스트 메일 발송 실패 (to=%s, error=%s)", mask_email(recipient), type(exc).__name__)
        await record_audit(
            db,
            actor_id=str(user.id),
            action="email.test",
            target_type="email",
            details={"to": mask_email(recipient), "success": False, "error_type": type(exc).__name__},
            request=request,
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="테스트 메일 발송에 실패했습니다. 관리자 로그를 확인하세요.",
        )

    await record_audit(
        db,
        actor_id=str(user.id),
        action="email.test",
        target_type="email",
        details={"to": mask_email(recipient), "success": True},
        request=request,
    )
    await db.commit()
    return {"message": f"테스트 메일이 {recipient}(으)로 발송되었습니다."}


@router.post("/users/{user_id}/resend-verification")
async def resend_verification_email(
    user_id: str,
    request: Request,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """사용자에게 인증 메일 재발송 — admin만 가능."""
    import secrets
    from datetime import datetime, timedelta, timezone
    from app.email.sender import send_email
    from app.email.templates import email_verification_template

    result = await db.execute(select(User).where(User.id == user_id).with_for_update())
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="유저를 찾을 수 없습니다.")

    if target_user.email_verified:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 인증된 사용자입니다.")

    if target_user.email_verify_code_required:
        code = issue_verification_code(target_user)
        subject = "[CTF Platform] 이메일 인증 코드"
        html = email_verification_template(target_user.username, verification_code=code)
        verification_method = "code"
    else:
        # 코드 도입 전 링크 인증 계정의 호환 경로
        token = secrets.token_urlsafe(48)
        target_user.email_verify_token = token
        target_user.email_verify_expires = datetime.now(timezone.utc) + timedelta(hours=settings.email_verify_expire_hours)
        verify_url = f"{settings.frontend_url}/verify-email?token={token}"
        subject = "[CTF Platform] 이메일 인증"
        html = email_verification_template(target_user.username, verify_url)
        verification_method = "legacy_link"

    try:
        await record_audit(
            db,
            actor_id=str(admin.id),
            action="user.resend_verification",
            target_type="user",
            target_id=str(target_user.id),
            details={"username": target_user.username, "method": verification_method},
            request=request,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("관리자 인증 재발송 상태 또는 감사 기록 저장 실패")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="인증 메일을 준비하지 못했습니다.",
        )

    try:
        await send_email(
            target_user.email,
            subject,
            html,
        )
    except Exception as exc:
        logger.warning(
            "관리자 인증 메일 재발송 실패 (user_id=%s, error_type=%s)",
            target_user.id,
            type(exc).__name__,
        )
        try:
            await record_audit(
                db,
                actor_id=str(admin.id),
                action="auth.verification_delivery_failed",
                target_type="user",
                target_id=str(target_user.id),
                details={"channel": "email", "admin_resend": True},
                request=request,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("관리자 인증 메일 재발송 실패 감사 기록 저장 실패")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="인증 메일 발송에 실패했습니다. SMTP 설정과 발송 로그를 확인하세요.",
        )
    return {"message": "인증 메일을 재발송했습니다."}


@router.patch("/users/{user_id}/verify")
async def admin_verify_user(
    user_id: str,
    request: Request,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """관리자가 사용자 이메일 인증 상태를 수동으로 전환 — admin만 가능."""
    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="유저를 찾을 수 없습니다.")

    if target_user.email_verified:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 인증된 사용자입니다.")

    clear_verification_artifacts(target_user)

    await record_audit(
        db,
        actor_id=str(admin.id),
        action="user.manual_verify",
        target_type="user",
        target_id=str(target_user.id),
        details={"username": target_user.username, "email": target_user.email},
        request=request,
    )
    await db.commit()
    return {"message": f"{target_user.username}의 이메일 인증이 완료되었습니다."}
