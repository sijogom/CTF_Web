"""문의 알림 발송 영수증 모델과 안전한 접근 경로 검증."""

import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.models.user import Base


_ALLOWED_INQUIRY_PATHS = frozenset({"/inquiry", "/inquiry/access"})
_TERMINAL_STATUSES = frozenset({"sent", "skipped"})


def is_safe_inquiry_access_path(value: str) -> bool:
    """Auth frontend origin에 결합해도 외부 URL이 될 수 없는 경로인지 확인한다.

    Community가 전달하는 proof는 fragment에만 들어갈 수 있다. URL 결합에
    ``urljoin``을 사용하지 않고, Auth가 검증된 상대 경로만 자체 frontend origin에
    붙이므로 외부 호스트로의 링크 전환을 허용하지 않는다.
    """
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    if value != value.strip() or any(character.isspace() for character in value):
        return False
    if "\\" in value or any(ord(character) < 32 for character in value):
        return False
    if not value.startswith("/") or value.startswith("//"):
        return False

    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query:
        return False
    if parsed.path not in _ALLOWED_INQUIRY_PATHS:
        return False
    return parsed.path != "/inquiry" or not parsed.fragment


class InquiryNotificationReceipt(Base):
    """Community outbox event의 Auth 측 idempotency 영수증.

    수신 이메일·사용자 ID·접근 proof·메일 본문은 보존하지 않는다. Community의
    outbox event ID만으로 delivery idempotency와 재시도를 추적한다.
    """

    __tablename__ = "inquiry_notification_receipts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), nullable=False)
    status = Column(String(20), nullable=False, default="processing")
    attempt_count = Column(Integer, nullable=False, default=0)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    terminal_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(100), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_inquiry_notification_receipts_event_id"),
        CheckConstraint(
            "status IN ('processing', 'retry', 'sent', 'skipped')",
            name="ck_inquiry_notification_receipts_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_inquiry_notification_receipts_attempt_count",
        ),
        Index(
            "ix_inquiry_notification_receipts_status_lease",
            "status",
            "lease_expires_at",
        ),
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES
