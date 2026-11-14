"""사용자 챌린지 등록 요청 모델 — 관리자 승인 전 대기 상태."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, Boolean, DateTime, Text, LargeBinary
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.models.course import Base


class ChallengeSubmission(Base):
    """사용자가 제출한 챌린지 등록 요청."""
    __tablename__ = "challenge_submissions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 제출자 정보
    submitter_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    submitter_name = Column(String(100), nullable=False)

    # 문제 메타데이터
    name = Column(String(200), nullable=False)
    category = Column(String(50), nullable=False)
    difficulty = Column(String(20), nullable=False)
    description = Column(Text, nullable=False, default="")
    value = Column(Integer, nullable=False, default=100)
    flag_type = Column(String(20), nullable=False, default="static")
    # 신규 행은 아래 암호문을 사용하고 이 호환 컬럼에는 고정 마커만 둔다.
    # 기존 행은 ``legacy-raw``로 식별되어 별도 보존/암호화 작업의 대상이 된다.
    flag_value = Column(String(500), nullable=False, default="")
    flag_ciphertext = Column(LargeBinary, nullable=True)
    flag_key_version = Column(String(64), nullable=True)
    flag_storage_version = Column(
        String(40),
        nullable=False,
        default="legacy-raw",
        server_default="legacy-raw",
    )
    flag_value_length = Column(Integer, nullable=False, default=0, server_default="0")
    tags = Column(JSONB, nullable=False, default=list)
    hints = Column(JSONB, nullable=False, default=list)  # [{"content": "...", "cost": 0}]

    # 파일 정보 (MinIO 경로 목록)
    file_paths = Column(JSONB, nullable=False, default=list)

    # 승인 상태: pending → approved / rejected
    status = Column(String(20), nullable=False, default="pending", index=True)
    reject_reason = Column(Text, nullable=True)
    reviewed_by = Column(UUID(as_uuid=True), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
