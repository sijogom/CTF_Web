"""account ban and appeal ledger

Revision ID: 20260813_001
Revises: 20260813_000
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260813_001"
down_revision = "20260813_000"
branch_labels = None
depends_on = None


_BAN_COLUMNS = {
    "id", "user_id", "public_reason", "evidence_summary", "policy_version", "source",
    "detection_rule_version", "started_at", "expires_at", "created_by", "created_at",
    "lifted_at", "lifted_by", "lift_reason", "appeal_allowed",
}
_APPEAL_COLUMNS = {
    "id", "ban_id", "user_id", "message", "status", "reviewer_id", "reviewer_note",
    "reviewed_at", "created_at",
}


def _assert_existing_columns(bind, table_name: str, required: set[str]) -> None:
    existing = {column["name"] for column in sa.inspect(bind).get_columns(table_name)}
    missing = sorted(required - existing)
    if missing:
        raise RuntimeError(
            f"{table_name} exists but is not compatible with this migration: missing {', '.join(missing)}"
        )


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "account_bans" not in tables:
        op.create_table(
            "account_bans",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("public_reason", sa.Text(), nullable=False),
            sa.Column("evidence_summary", sa.Text(), nullable=False),
            sa.Column("policy_version", sa.String(length=100), nullable=False),
            sa.Column("source", sa.String(length=20), nullable=False, server_default="manual"),
            sa.Column("detection_rule_version", sa.String(length=100), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("lifted_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("lift_reason", sa.Text(), nullable=True),
            sa.Column("appeal_allowed", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.CheckConstraint("source IN ('manual', 'automated')", name="ck_account_bans_source"),
            sa.CheckConstraint(
                "expires_at IS NULL OR expires_at > started_at",
                name="ck_account_bans_expiry_after_start",
            ),
        )
    else:
        _assert_existing_columns(bind, "account_bans", _BAN_COLUMNS)

    if "ban_appeals" not in tables:
        op.create_table(
            "ban_appeals",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("ban_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("reviewer_note", sa.Text(), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.CheckConstraint(
                "status IN ('pending', 'accepted', 'rejected')",
                name="ck_ban_appeals_status",
            ),
        )
    else:
        _assert_existing_columns(bind, "ban_appeals", _APPEAL_COLUMNS)

    op.execute("CREATE INDEX IF NOT EXISTS ix_account_bans_user_id ON account_bans (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_account_bans_started_at ON account_bans (started_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_account_bans_expires_at ON account_bans (expires_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_account_bans_created_by ON account_bans (created_by)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_account_bans_lifted_at ON account_bans (lifted_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_account_bans_user_started ON account_bans (user_id, started_at)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_account_bans_active_lookup "
        "ON account_bans (user_id, started_at, expires_at) WHERE lifted_at IS NULL"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ban_appeals_ban_id ON ban_appeals (ban_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ban_appeals_user_id ON ban_appeals (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ban_appeals_status ON ban_appeals (status)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_ban_appeals_one_pending_per_ban "
        "ON ban_appeals (ban_id) WHERE status = 'pending'"
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table_name in ("ban_appeals", "account_bans"):
        if table_name in tables:
            count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
            if count:
                raise RuntimeError(f"{table_name} contains ledger data; restore from backup instead of downgrade")
    if "ban_appeals" in tables:
        op.drop_table("ban_appeals")
    if "account_bans" in tables:
        op.drop_table("account_bans")
