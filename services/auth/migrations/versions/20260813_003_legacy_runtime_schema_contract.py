"""formalize formerly runtime Auth schema additions

Revision ID: 20260813_003
Revises: 20260813_002
Create Date: 2026-08-13

The legacy Auth worker historically used ``create_all`` plus inline
``ALTER TABLE ... IF NOT EXISTS`` statements.  This forward migration makes
those known additions part of the Compose migration path.  It never drops
data and rejects an interrupted multi-column feature group instead of guessing
how to repair it.
"""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260813_003"
down_revision = "20260813_002"
branch_labels = None
depends_on = None


_CORE_TABLES = frozenset({"users", "competitions", "competition_challenges"})


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _require_core_tables() -> None:
    missing = _CORE_TABLES - _table_names()
    if missing:
        raise RuntimeError(
            "Auth legacy runtime schema migration 전에 필요한 core table이 없습니다: "
            f"{', '.join(sorted(missing))}."
        )


def _ensure_feature_columns(
    table_name: str,
    feature_name: str,
    columns: Iterable[sa.Column],
) -> None:
    """Add a whole known feature group or reject a partially applied group."""

    columns = tuple(columns)
    required = {column.name for column in columns}
    existing = _column_names(table_name)
    present = existing.intersection(required)
    if present and present != required:
        raise RuntimeError(
            f"Auth {feature_name} schema가 부분 생성 상태입니다: "
            f"현재 {', '.join(sorted(present))}, 필수 {', '.join(sorted(required))}."
        )
    if present == required:
        return
    for column in columns:
        op.add_column(table_name, column)


def _has_unique_single_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints(table_name):
        if set(constraint.get("column_names") or []) == {column_name}:
            return True
    for index in inspector.get_indexes(table_name):
        if index.get("unique") and set(index.get("column_names") or []) == {column_name}:
            return True
    return False


def _ensure_unique_single_column_index(
    table_name: str,
    index_name: str,
    column_name: str,
) -> None:
    if not _has_unique_single_column(table_name, column_name):
        op.create_index(index_name, table_name, [column_name], unique=True)


def _ensure_admin_settings() -> None:
    tables = _table_names()
    required = {"key", "value", "updated_at"}
    if "admin_settings" not in tables:
        op.create_table(
            "admin_settings",
            sa.Column("key", sa.String(length=100), primary_key=True, nullable=False),
            sa.Column("value", sa.Text(), nullable=False, server_default="{}"),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.text("NOW()"),
            ),
        )
        return

    missing = required - _column_names("admin_settings")
    if missing:
        raise RuntimeError(
            "admin_settings가 존재하지만 legacy compatibility contract가 불완전합니다: "
            f"{', '.join(sorted(missing))}."
        )


def _ensure_competition_deletion_confirmations() -> None:
    table_name = "competition_deletion_confirmations"
    required = {
        "id",
        "competition_id",
        "actor_id",
        "token_hash",
        "reason",
        "expires_at",
        "used_at",
        "created_at",
    }
    tables = _table_names()
    if table_name not in tables:
        op.create_table(
            table_name,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "competition_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("competitions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "actor_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("NOW()"),
            ),
        )
    else:
        missing = required - _column_names(table_name)
        if missing:
            raise RuntimeError(
                "competition_deletion_confirmations가 부분 생성 상태입니다: "
                f"{', '.join(sorted(missing))}."
            )

    _ensure_unique_single_column_index(
        table_name,
        "uq_competition_deletion_confirmations_token_hash",
        "token_hash",
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "ix_competition_deletion_confirmations_competition_id "
        "ON competition_deletion_confirmations (competition_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "ix_competition_deletion_confirmations_actor_id "
        "ON competition_deletion_confirmations (actor_id)"
    )


def upgrade() -> None:
    _require_core_tables()

    _ensure_feature_columns(
        "users",
        "Discord identity",
        (
            sa.Column("discord_id", sa.String(length=50), nullable=True),
            sa.Column("discord_username", sa.String(length=100), nullable=True),
        ),
    )
    _ensure_unique_single_column_index(
        "users",
        "ix_users_discord_id",
        "discord_id",
    )

    _ensure_feature_columns(
        "users",
        "Baekjoon identity",
        (
            sa.Column("baekjoon_handle", sa.String(length=50), nullable=True),
            sa.Column("baekjoon_tier", sa.Integer(), nullable=True),
            sa.Column("tier_override", sa.String(length=50), nullable=True),
        ),
    )
    _ensure_unique_single_column_index(
        "users",
        "ix_users_baekjoon_handle",
        "baekjoon_handle",
    )

    _ensure_feature_columns(
        "users",
        "email notification preference",
        (
            sa.Column(
                "email_notifications",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        ),
    )
    _ensure_feature_columns(
        "users",
        "email verification code",
        (
            sa.Column(
                "email_verify_code_required",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("email_verify_code_hash", sa.String(length=128), nullable=True),
            sa.Column("email_verify_code_expires", sa.DateTime(timezone=True), nullable=True),
            sa.Column("email_verify_code_sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "email_verify_code_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        ),
    )

    _ensure_feature_columns(
        "competition_challenges",
        "competition scoring",
        (
            sa.Column(
                "scoring_type",
                sa.String(length=10),
                nullable=False,
                server_default="static",
            ),
            sa.Column(
                "initial_value",
                sa.Integer(),
                nullable=False,
                server_default="500",
            ),
            sa.Column("decay", sa.Integer(), nullable=False, server_default="100"),
            sa.Column(
                "minimum_value",
                sa.Integer(),
                nullable=False,
                server_default="50",
            ),
        ),
    )
    _ensure_feature_columns(
        "competitions",
        "competition soft delete",
        (
            sa.Column(
                "is_deleted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("deletion_reason", sa.Text(), nullable=True),
            sa.Column(
                "published_before_deletion",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        ),
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_competitions_is_deleted "
        "ON competitions (is_deleted)"
    )

    _ensure_admin_settings()
    _ensure_competition_deletion_confirmations()


def downgrade() -> None:
    raise RuntimeError(
        "Auth legacy runtime schema migration은 데이터 보존 경계입니다. "
        "자동 downgrade 대신 검증된 백업 복원을 사용하세요."
    )
