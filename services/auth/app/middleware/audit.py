"""감사 로그 유틸리티."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog


async def log_security_event(
    db: AsyncSession,
    action: str,
    actor_id: str | UUID | None = None,
    target_type: str = "session",
    target_id: str | None = None,
    details: dict | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    """보안 이벤트를 audit_logs 테이블에 기록.

    Args:
        db: 데이터베이스 세션
        action: 수행된 액션 (e.g., "USER_ROLE_CHANGED", "CHALLENGE_DELETED", "ACCOUNT_DISABLED")
        actor_id: 액션을 수행한 사용자 ID (관리자)
        target_type: 대상 유형 (e.g., "user", "challenge", "session")
        target_id: 대상 ID
        details: 상세 정보 (JSON)
        ip_address: 요청한 클라이언트 IP

    Returns:
        생성된 AuditLog 인스턴스
    """
    log = AuditLog(
        actor_id=str(actor_id) if actor_id else None,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details or {},
        ip_address=ip_address,
    )
    db.add(log)
    await db.flush()
    return log
