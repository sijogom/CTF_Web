"""Add the atomic marketplace transfer ledger without changing existing points.

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
        "marketplace_point_transfers",
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("buyer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seller_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("price_paid", sa.Integer(), nullable=False),
        sa.Column("seller_earned", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("failure_code", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("price_paid > 0 AND seller_earned > 0 AND seller_earned <= price_paid", name="ck_marketplace_transfer_amounts"),
        sa.CheckConstraint("buyer_id <> seller_id", name="ck_marketplace_transfer_parties"),
        sa.CheckConstraint("state IN ('processing', 'completed', 'rejected')", name="ck_marketplace_transfer_state"),
    )


def downgrade() -> None:
    raise RuntimeError("장터 이체 원장은 삭제하지 않습니다. 이전 image rollback은 추가 schema를 유지하세요.")
