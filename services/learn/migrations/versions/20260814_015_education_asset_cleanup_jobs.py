"""교육 private object 삭제의 durable cleanup job을 추가한다."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260814_015"
down_revision: Union[str, None] = "20260814_014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "education_asset_cleanup_jobs"
_REQUIRED_COLUMNS = {
    "id",
    "asset_id",
    "object_path",
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
    tables = set(_inspector().get_table_names())
    if "education_assets" not in tables:
        raise RuntimeError(
            "education_assets가 없어 교육 자료 cleanup job migration을 적용할 수 없습니다."
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
            f"교육 자료 cleanup job schema가 부분 상태입니다: missing={','.join(sorted(missing))}"
        )
    return True


def upgrade() -> None:
    _assert_prerequisite_tables()
    if _is_complete():
        return

    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("education_assets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("object_path", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("asset_id", name="uq_education_asset_cleanup_jobs_asset"),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','failed')",
            name="ck_education_asset_cleanup_jobs_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_education_asset_cleanup_jobs_attempt_nonnegative",
        ),
    )
    op.create_index(
        "ix_education_asset_cleanup_jobs_claim",
        _TABLE,
        ["status", "available_at"],
    )
    op.create_index(
        "ix_education_asset_cleanup_jobs_lease",
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
            "교육 자료 cleanup job이 남아 있어 destructive downgrade를 중단합니다."
        )
    op.drop_index("ix_education_asset_cleanup_jobs_lease", table_name=_TABLE)
    op.drop_index("ix_education_asset_cleanup_jobs_claim", table_name=_TABLE)
    op.drop_table(_TABLE)
