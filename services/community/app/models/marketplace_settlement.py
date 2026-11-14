"""구매 시도와 정산 outbox. 외부 이체 전에 commit하는 복구 기준이다."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID

from app.models.post import Base


class MarketplacePurchaseOperation(Base):
    __tablename__ = "marketplace_purchase_operations"
    __table_args__ = (
        CheckConstraint("state IN ('processing', 'completed', 'rejected')", name="ck_marketplace_operation_state"),
        CheckConstraint("price_paid > 0 AND seller_earned > 0 AND seller_earned <= price_paid", name="ck_marketplace_operation_amounts"),
        CheckConstraint("buyer_id <> seller_id", name="ck_marketplace_operation_parties"),
        Index("uq_marketplace_operation_active", "item_id", "buyer_id", unique=True, postgresql_where=text("state IN ('processing', 'completed')")),
        Index("ix_marketplace_operation_lookup", "item_id", "buyer_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    item_id = Column(UUID(as_uuid=True), nullable=False)
    buyer_id = Column(UUID(as_uuid=True), nullable=False)
    seller_id = Column(UUID(as_uuid=True), nullable=False)
    price_paid = Column(Integer, nullable=False)
    seller_earned = Column(Integer, nullable=False)
    payload_digest = Column(String(64), nullable=False)
    state = Column(String(20), nullable=False, default="processing")
    failure_code = Column(String(40), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    settled_at = Column(DateTime(timezone=True), nullable=True)


class MarketplaceSettlementOutbox(Base):
    __tablename__ = "marketplace_settlement_outbox"
    __table_args__ = (
        CheckConstraint("state IN ('pending', 'leased', 'done', 'blocked')", name="ck_marketplace_settlement_state"),
        CheckConstraint("attempt_count >= 0", name="ck_marketplace_settlement_attempts"),
        Index("ix_marketplace_settlement_ready", "state", "available_at", "lease_expires_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation_id = Column(UUID(as_uuid=True), ForeignKey("marketplace_purchase_operations.id", ondelete="RESTRICT"), unique=True, nullable=False)
    state = Column(String(20), nullable=False, default="pending")
    attempt_count = Column(Integer, nullable=False, default=0)
    available_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    lease_token = Column(UUID(as_uuid=True), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(40), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
