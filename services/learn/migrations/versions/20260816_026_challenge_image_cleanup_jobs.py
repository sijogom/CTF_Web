"""문제 삭제와 Docker image tag 정리를 위한 durable outbox."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260816_026"
down_revision: Union[str, None] = "20260816_025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "challenge_image_cleanup_jobs"
_REQUIRED_COLUMNS = {
    "id",
    "challenge_id",
    "image_reference",
    "status",
    "attempt_count",
    "available_at",
    "lease_until",
    "completed_at",
    "last_error_code",
    "created_at",
    "updated_at",
}


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _assert_prerequisite_tables() -> None:
    if "challenges" not in set(_inspector().get_table_names()):
        raise RuntimeError(
            "challenges 테이블이 없어 문제 이미지 cleanup job migration을 적용할 수 없습니다."
        )


def _is_complete() -> bool:
    inspector = _inspector()
    tables = set(inspector.get_table_names())
    if _TABLE not in tables:
        return False
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    missing = _REQUIRED_COLUMNS - columns
    if missing:
        raise RuntimeError(
            "문제 이미지 cleanup job schema가 부분 상태입니다: "
            f"missing={','.join(sorted(missing))}"
        )
    return True


def upgrade() -> None:
    _assert_prerequisite_tables()
    if _is_complete():
        return

    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Challenge 삭제 뒤에도 원장과 정리 대상 식별자를 유지하므로 FK를 두지 않는다.
        sa.Column("challenge_id", sa.Integer(), nullable=False),
        sa.Column("image_reference", sa.String(length=500), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint(
            "challenge_id",
            "image_reference",
            name="uq_challenge_image_cleanup_jobs_target",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','failed')",
            name="ck_challenge_image_cleanup_jobs_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_challenge_image_cleanup_jobs_attempt_nonnegative",
        ),
    )
    op.create_index(
        "ix_challenge_image_cleanup_jobs_challenge_id",
        _TABLE,
        ["challenge_id"],
    )
    op.create_index(
        "ix_challenge_image_cleanup_jobs_status",
        _TABLE,
        ["status"],
    )
    op.create_index(
        "ix_challenge_image_cleanup_jobs_available_at",
        _TABLE,
        ["available_at"],
    )
    op.create_index(
        "ix_challenge_image_cleanup_jobs_claim",
        _TABLE,
        ["status", "available_at"],
    )
    op.create_index(
        "ix_challenge_image_cleanup_jobs_lease",
        _TABLE,
        ["status", "lease_until"],
    )


def downgrade() -> None:
    tables = set(_inspector().get_table_names())
    if _TABLE not in tables:
        return
    count = int(op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {_TABLE}")).scalar_one())
    if count:
        raise RuntimeError(
            "문제 이미지 cleanup job이 남아 있어 destructive downgrade를 중단합니다."
        )
    for index_name in (
        "ix_challenge_image_cleanup_jobs_lease",
        "ix_challenge_image_cleanup_jobs_claim",
        "ix_challenge_image_cleanup_jobs_available_at",
        "ix_challenge_image_cleanup_jobs_status",
        "ix_challenge_image_cleanup_jobs_challenge_id",
    ):
        op.drop_index(index_name, table_name=_TABLE)
    op.drop_table(_TABLE)
