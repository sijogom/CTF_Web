"""inquiry notification delivery receipts

Revision ID: 20260813_002
Revises: 20260813_001
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260813_002"
down_revision = "20260813_001"
branch_labels = None
depends_on = None


_REQUIRED_COLUMNS = {
    "id",
    "event_id",
    "status",
    "attempt_count",
    "lease_expires_at",
    "terminal_at",
    "last_error",
    "created_at",
    "updated_at",
}


def _assert_existing_columns(bind, table_name: str, required: set[str]) -> None:
    existing = {column["name"] for column in sa.inspect(bind).get_columns(table_name)}
    missing = sorted(required - existing)
    if missing:
        raise RuntimeError(
            f"{table_name} exists but is not compatible with this migration: "
            f"missing {', '.join(missing)}"
        )


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "inquiry_notification_receipts" not in tables:
        op.create_table(
            "inquiry_notification_receipts",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="processing"),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.String(length=100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.CheckConstraint(
                "status IN ('processing', 'retry', 'sent', 'skipped')",
                name="ck_inquiry_notification_receipts_status",
            ),
            sa.CheckConstraint(
                "attempt_count >= 0",
                name="ck_inquiry_notification_receipts_attempt_count",
            ),
            sa.UniqueConstraint("event_id", name="uq_inquiry_notification_receipts_event_id"),
        )
    else:
        _assert_existing_columns(bind, "inquiry_notification_receipts", _REQUIRED_COLUMNS)

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_inquiry_notification_receipts_event_id "
        "ON inquiry_notification_receipts (event_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_inquiry_notification_receipts_status_lease "
        "ON inquiry_notification_receipts (status, lease_expires_at)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "inquiry_notification_receipts" not in tables:
        return
    count = bind.execute(sa.text("SELECT COUNT(*) FROM inquiry_notification_receipts")).scalar_one()
    if count:
        raise RuntimeError(
            "inquiry_notification_receipts contains delivery receipts; "
            "restore from backup instead of downgrade"
        )
    op.drop_table("inquiry_notification_receipts")
