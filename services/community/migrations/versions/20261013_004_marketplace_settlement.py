"""Add durable marketplace operations and their settlement outbox.

Revision ID: 20261013_004
Revises: 20260813_003
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261013_004"
down_revision = "20260813_003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "marketplace_purchase_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("buyer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seller_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("price_paid", sa.Integer(), nullable=False),
        sa.Column("seller_earned", sa.Integer(), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("failure_code", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state IN ('processing', 'completed', 'rejected')", name="ck_marketplace_operation_state"),
        sa.CheckConstraint("price_paid > 0 AND seller_earned > 0 AND seller_earned <= price_paid", name="ck_marketplace_operation_amounts"),
        sa.CheckConstraint("buyer_id <> seller_id", name="ck_marketplace_operation_parties"),
    )
    op.create_index("uq_marketplace_operation_active", "marketplace_purchase_operations", ["item_id", "buyer_id"], unique=True, postgresql_where=sa.text("state IN ('processing', 'completed')"))
    op.create_index("ix_marketplace_operation_lookup", "marketplace_purchase_operations", ["item_id", "buyer_id", "created_at"])
    op.create_table(
        "marketplace_settlement_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("marketplace_purchase_operations.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('pending', 'leased', 'done', 'blocked')", name="ck_marketplace_settlement_state"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_marketplace_settlement_attempts"),
    )
    op.create_index("ix_marketplace_settlement_ready", "marketplace_settlement_outbox", ["state", "available_at", "lease_expires_at"])


def downgrade() -> None:
    raise RuntimeError("장터 구매·정산 원장은 삭제하지 않습니다. 이전 image rollback은 추가 schema를 유지하세요.")
