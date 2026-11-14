"""Create the course deletion-confirmation and audit tables if absent.

These two ORM tables predate the versioned migration path and were previously
created only by the application worker's ``create_all``.  Keep the migration
additive and fail closed for a partial pair so deletion/audit cannot silently
run against an incomplete schema.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260814_022"
down_revision = "20260814_021"
branch_labels = None
depends_on = None

_CONFIRMATIONS = "course_deletion_confirmations"
_AUDITS = "course_audit_events"
_TABLES = {_CONFIRMATIONS, _AUDITS}
_REQUIRED_COLUMNS = {
    _CONFIRMATIONS: {
        "id",
        "course_id",
        "actor_id",
        "token_hash",
        "reason",
        "expires_at",
        "used_at",
        "created_at",
    },
    _AUDITS: {"id", "course_id", "actor_id", "action", "details", "created_at"},
}


def _assert_complete_or_absent() -> bool:
    inspector = sa.inspect(op.get_bind())
    present = set(inspector.get_table_names()).intersection(_TABLES)
    if not present:
        return False
    if present != _TABLES:
        raise RuntimeError(
            "course moderation ledger tables are partially present; refusing migration: "
            f"{', '.join(sorted(present))}"
        )
    missing: list[str] = []
    for table_name, required in _REQUIRED_COLUMNS.items():
        actual = {column["name"] for column in inspector.get_columns(table_name)}
        missing.extend(f"{table_name}.{column}" for column in sorted(required - actual))
    if missing:
        raise RuntimeError(
            "course moderation ledger schema is incomplete; refusing migration: "
            f"{', '.join(missing)}"
        )
    return True


def _create_confirmations() -> None:
    op.create_table(
        _CONFIRMATIONS,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            name="fk_course_deletion_confirmations_course",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_course_deletion_confirmations"),
        sa.UniqueConstraint("token_hash", name="uq_course_deletion_confirmations_token_hash"),
    )
    op.create_index(
        "ix_course_deletion_confirmations_course_id",
        _CONFIRMATIONS,
        ["course_id"],
    )
    op.create_index(
        "ix_course_deletion_confirmations_actor_id",
        _CONFIRMATIONS,
        ["actor_id"],
    )
    op.create_index(
        "ix_course_deletion_confirmations_expires_at",
        _CONFIRMATIONS,
        ["expires_at"],
    )


def _create_audits() -> None:
    op.create_table(
        _AUDITS,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            name="fk_course_audit_events_course",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_course_audit_events"),
    )
    op.create_index("ix_course_audit_events_course_id", _AUDITS, ["course_id"])
    op.create_index("ix_course_audit_events_actor_id", _AUDITS, ["actor_id"])
    op.create_index("ix_course_audit_events_action", _AUDITS, ["action"])
    op.create_index("ix_course_audit_events_created_at", _AUDITS, ["created_at"])


def upgrade() -> None:
    if _assert_complete_or_absent():
        return
    _create_confirmations()
    _create_audits()


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _TABLES.issubset(set(inspector.get_table_names())):
        return
    for table_name in _TABLES:
        if op.get_bind().exec_driver_sql(f"SELECT 1 FROM {table_name} LIMIT 1").first():
            raise RuntimeError("course moderation ledger downgrade is destructive while rows exist")
    op.drop_index("ix_course_audit_events_created_at", table_name=_AUDITS)
    op.drop_index("ix_course_audit_events_action", table_name=_AUDITS)
    op.drop_index("ix_course_audit_events_actor_id", table_name=_AUDITS)
    op.drop_index("ix_course_audit_events_course_id", table_name=_AUDITS)
    op.drop_table(_AUDITS)
    op.drop_index("ix_course_deletion_confirmations_expires_at", table_name=_CONFIRMATIONS)
    op.drop_index("ix_course_deletion_confirmations_actor_id", table_name=_CONFIRMATIONS)
    op.drop_index("ix_course_deletion_confirmations_course_id", table_name=_CONFIRMATIONS)
    op.drop_table(_CONFIRMATIONS)
