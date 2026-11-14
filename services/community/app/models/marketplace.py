"""장터(마켓플레이스) DB 모델."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, DateTime, Text, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.models.post import Base


class MarketplaceItem(Base):
    """장터 아이템."""
    __tablename__ = "marketplace_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False, default="")
    price = Column(Integer, nullable=False)  # 포인트 가격
    file_url = Column(String(500), nullable=True)  # MinIO 경로
    original_filename = Column(String(255), nullable=True)
    seller_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    seller_name = Column(String(50), nullable=False)
    category = Column(String(20), nullable=False, default="etc")  # exam, material, etc
    status = Column(String(20), nullable=False, default="active")  # active, hidden
    download_count = Column(Integer, default=0, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class MarketplacePurchase(Base):
    """장터 구매 기록."""
    __tablename__ = "marketplace_purchases"
    __table_args__ = (
        UniqueConstraint("item_id", "buyer_id", name="uq_purchase_item_buyer"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    item_id = Column(
        UUID(as_uuid=True),
        ForeignKey("marketplace_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    buyer_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    price_paid = Column(Integer, nullable=False)  # 구매 시 차감 포인트
    seller_earned = Column(Integer, nullable=False)  # 판매자에게 지급된 포인트 (20%)
    purchased_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class MarketplaceComment(Base):
    """장터 아이템 댓글."""
    __tablename__ = "marketplace_comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    item_id = Column(
        UUID(as_uuid=True),
        ForeignKey("marketplace_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    author_name = Column(String(50), nullable=False)
    content = Column(Text, nullable=False)
    is_anonymous = Column(Boolean, nullable=False, default=False)
    is_hidden = Column(Boolean, nullable=False, default=False)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(DateTime(timezone=True), nullable=True)
