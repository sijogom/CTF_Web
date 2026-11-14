"""삭제된 문제의 소유 runtime 자원 정리를 위한 durable outbox 모델."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.models.course import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ChallengeImageCleanupJob(Base):
    """문제 삭제 커밋과 컨테이너·포트·Redis·Docker tag 정리를 분리하는 원장.

    ``challenge_id``는 Challenge 삭제 후에도 감사·정리 식별자로 남아야 하므로
    FK를 두지 않는다. ``image_reference``는 플랫폼이 생성한 정확한 tag 또는
    명시적인 image 삭제 대상이 없음을 뜻하는 빈 문자열만 저장한다. 기존 schema와 image 전용
    정리 기록은 유지하며 worker는 job UUID와 문제 ID를 Env에 전달한다.
    """

    __tablename__ = "challenge_image_cleanup_jobs"
    __table_args__ = (
        UniqueConstraint(
            "challenge_id",
            "image_reference",
            name="uq_challenge_image_cleanup_jobs_target",
        ),
        CheckConstraint(
            "status IN ('pending','processing','completed','failed')",
            name="ck_challenge_image_cleanup_jobs_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_challenge_image_cleanup_jobs_attempt_nonnegative",
        ),
        Index(
            "ix_challenge_image_cleanup_jobs_claim",
            "status",
            "available_at",
        ),
        Index(
            "ix_challenge_image_cleanup_jobs_lease",
            "status",
            "lease_until",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Challenge 삭제 뒤에도 정리 대상 식별자로 보존한다. 외부 입력을 그대로
    # image_reference로 사용하지 않고 API route/worker에서 exact tag를 검증한다.
    challenge_id = Column(Integer, nullable=False, index=True)
    image_reference = Column(String(500), nullable=False)
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    available_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now, index=True)
    lease_until = Column(DateTime(timezone=True), nullable=True, index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    last_error_code = Column(String(80), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
    )
