"""Create Community inquiry guest-access and notification-outbox tables.

Revision ID: 20260813_003
Revises: 20260813_002
Create Date: 2026-08-13

Existing inquiries are not modified or backfilled. A partially-created ticket
ledger is rejected instead of being repaired automatically.

The additive ``inquiries.locale`` field keeps the sender's selected UI locale
for later guest links and reply notifications. Existing rows retain the safe
``ko`` server default; no inquiry content or recipient data is transformed.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260813_003"
down_revision: Union[str, None] = "20260813_002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLES = frozenset(
    {
        "inquiry_access_challenges",
        "inquiry_access_sessions",
        "inquiry_notification_outbox",
    }
)

EXPECTED_COLUMNS = {
    "inquiry_access_challenges": {
        "id",
        "inquiry_id",
        "purpose",
        "expires_at",
        "used_at",
        "created_at",
    },
    "inquiry_access_sessions": {
        "id",
        "inquiry_id",
        "token_digest",
        "expires_at",
        "revoked_at",
        "last_used_at",
        "created_at",
    },
    "inquiry_notification_outbox": {
        "id",
        "inquiry_id",
        "reply_id",
        "challenge_id",
        "recipient_type",
        "recipient_user_id",
        "kind",
        "locale",
        "dedupe_key",
        "state",
        "attempt_count",
        "available_at",
        "lease_token_digest",
        "lease_owner",
        "lease_expires_at",
        "last_error",
        "sent_at",
        "created_at",
        "updated_at",
    },
}


def _ensure_inquiry_locale(bind) -> None:
    """Add the non-content locale metadata once, refusing an incompatible column."""
    columns = {
        column["name"]: column
        for column in sa.inspect(bind).get_columns("inquiries")
    }
    locale = columns.get("locale")
    if locale is None:
        op.add_column(
            "inquiries",
            sa.Column(
                "locale",
                sa.String(length=10),
                nullable=False,
                server_default="ko",
            ),
        )
        return
    if locale.get("nullable") or "char" not in str(locale["type"]).lower():
        raise RuntimeError(
            "inquiries.locale exists but is not compatible with inquiry delivery"
        )


def _table_names(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _assert_existing_contract(bind) -> None:
    inspector = sa.inspect(bind)
    for table_name, expected in EXPECTED_COLUMNS.items():
        actual = {column["name"] for column in inspector.get_columns(table_name)}
        missing = sorted(expected - actual)
        if missing:
            raise RuntimeError(
                f"{table_name} exists but is not compatible with this migration: "
                f"missing {', '.join(missing)}"
            )


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    )


def _create_tables() -> None:
    op.create_table(
        "inquiry_access_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "inquiry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inquiries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(length=20), nullable=False, server_default="guest_access"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "purpose IN ('guest_access', 'reply_access')",
            name="ck_inquiry_access_challenges_purpose",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_inquiry_access_challenges_expiry",
        ),
    )
    op.create_index(
        "ix_inquiry_access_challenges_inquiry_id",
        "inquiry_access_challenges",
        ["inquiry_id"],
    )
    op.create_index(
        "ix_inquiry_access_challenges_expires_at",
        "inquiry_access_challenges",
        ["expires_at"],
    )
    op.create_index(
        "ix_inquiry_access_challenges_inquiry_expires",
        "inquiry_access_challenges",
        ["inquiry_id", "expires_at"],
    )

    op.create_table(
        "inquiry_access_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "inquiry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inquiries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_digest", sa.String(length=64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_inquiry_access_sessions_expiry",
        ),
    )
    op.create_index(
        "ix_inquiry_access_sessions_inquiry_id",
        "inquiry_access_sessions",
        ["inquiry_id"],
    )
    op.create_index(
        "ix_inquiry_access_sessions_expires_at",
        "inquiry_access_sessions",
        ["expires_at"],
    )
    op.create_index(
        "ix_inquiry_access_sessions_inquiry_expires",
        "inquiry_access_sessions",
        ["inquiry_id", "expires_at"],
    )

    op.create_table(
        "inquiry_notification_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "inquiry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inquiries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reply_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inquiry_replies.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "challenge_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inquiry_access_challenges.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("recipient_type", sa.String(length=20), nullable=False),
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("locale", sa.String(length=10), nullable=False, server_default="ko"),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False, unique=True),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("lease_token_digest", sa.String(length=64), nullable=True),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "recipient_type IN ('guest', 'member')",
            name="ck_inquiry_notification_outbox_recipient_type",
        ),
        sa.CheckConstraint(
            "kind IN ('guest_access', 'reply_available')",
            name="ck_inquiry_notification_outbox_kind",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'processing', 'retry', 'sent', 'skipped')",
            name="ck_inquiry_notification_outbox_state",
        ),
        sa.CheckConstraint(
            "(recipient_type = 'guest' AND recipient_user_id IS NULL) "
            "OR (recipient_type = 'member' AND recipient_user_id IS NOT NULL)",
            name="ck_inquiry_notification_outbox_recipient_ref",
        ),
    )
    op.create_index(
        "ix_inquiry_notification_outbox_inquiry_id",
        "inquiry_notification_outbox",
        ["inquiry_id"],
    )
    op.create_index(
        "ix_inquiry_notification_outbox_reply_id",
        "inquiry_notification_outbox",
        ["reply_id"],
    )
    op.create_index(
        "ix_inquiry_notification_outbox_challenge_id",
        "inquiry_notification_outbox",
        ["challenge_id"],
    )
    op.create_index(
        "ix_inquiry_notification_outbox_recipient_user_id",
        "inquiry_notification_outbox",
        ["recipient_user_id"],
    )
    op.create_index(
        "ix_inquiry_notification_outbox_state",
        "inquiry_notification_outbox",
        ["state"],
    )
    op.create_index(
        "ix_inquiry_notification_outbox_available_at",
        "inquiry_notification_outbox",
        ["available_at"],
    )
    op.create_index(
        "ix_inquiry_notification_outbox_state_available",
        "inquiry_notification_outbox",
        ["state", "available_at"],
    )


def upgrade() -> None:
    bind = op.get_bind()
    present = _table_names(bind) & TABLES
    if present and present != TABLES:
        missing = ", ".join(sorted(TABLES - present))
        raise RuntimeError(
            "Community inquiry ticket ledger is partial; refusing automatic repair. "
            f"Missing tables: {missing}"
        )
    _ensure_inquiry_locale(bind)
    if not present:
        _create_tables()
        return
    _assert_existing_contract(bind)


def downgrade() -> None:
    bind = op.get_bind()
    present = _table_names(bind) & TABLES
    for table_name in sorted(present):
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
        if count:
            raise RuntimeError(
                "Community inquiry ticket ledger contains access or delivery data; "
                "restore from an approved backup instead of downgrade."
            )
    for table_name in (
        "inquiry_notification_outbox",
        "inquiry_access_sessions",
        "inquiry_access_challenges",
    ):
        if table_name in present:
            op.drop_table(table_name)
