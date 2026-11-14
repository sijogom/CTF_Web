"""장터 이체의 멱등 결과 원장. 사용자 삭제 이후에도 정산 근거를 보존한다."""

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from app.models.user import Base


class MarketplacePointTransfer(Base):
    __tablename__ = "marketplace_point_transfers"
    __table_args__ = (
        CheckConstraint("price_paid > 0 AND seller_earned > 0 AND seller_earned <= price_paid", name="ck_marketplace_transfer_amounts"),
        CheckConstraint("buyer_id <> seller_id", name="ck_marketplace_transfer_parties"),
        CheckConstraint("state IN ('processing', 'completed', 'rejected')", name="ck_marketplace_transfer_state"),
    )

    operation_id = Column(UUID(as_uuid=True), primary_key=True)
    payload_digest = Column(String(64), nullable=False)
    buyer_id = Column(UUID(as_uuid=True), nullable=False)
    seller_id = Column(UUID(as_uuid=True), nullable=False)
    price_paid = Column(Integer, nullable=False)
    seller_earned = Column(Integer, nullable=False)
    state = Column(String(20), nullable=False)
    failure_code = Column(String(40), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    settled_at = Column(DateTime(timezone=True), nullable=True)
