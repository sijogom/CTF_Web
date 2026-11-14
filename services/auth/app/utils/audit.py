"""감사 로그 기록 유틸리티."""

import logging

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog

logger = logging.getLogger(__name__)


def _extract_ip(request: Request) -> str:
    """클라이언트 IP 추출 (프록시 헤더 우선)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def record_audit(
    db: AsyncSession,
    *,
    actor_id: str | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict | None = None,
    request: Request | None = None,
) -> None:
    """감사 로그를 DB에 기록한다.

    Args:
        db: DB 세션
        actor_id: 작업 수행자 UUID (시스템 작업이면 None)
        action: 작업 종류 (예: "user.role_change", "user.delete")
        target_type: 대상 유형 (예: "user", "settings")
        target_id: 대상 식별자
        details: 추가 정보 (JSON)
        request: FastAPI Request (IP 추출용)
    """
    ip = _extract_ip(request) if request else None

    log = AuditLog(
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
        ip_address=ip,
    )
    db.add(log)
    # flush로 즉시 반영 (caller가 commit할 때 함께 저장)
    await db.flush()
