"""레슨 본문 이미지의 참조·고아 후보·정리 상태 원장을 추가한다."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260814_016"
down_revision: Union[str, None] = "20260814_015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "lesson_image_assets"
_REQUIRED_COLUMNS = {
    "id",
    "object_path",
    "folder_id",
    "filename",
    "original_name",
    "uploaded_by",
    "state",
    "created_at",
    "last_seen_at",
    "cleanup_after",
    "deleted_at",
    "attempt_count",
    "lease_until",
    "last_error_code",
}


def _table_names(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _table_names(bind)
    # 000/legacy clean bootstrap may not have created the core lesson table yet.
    # Do not create a detached image table in that state.
    if "lessons" not in tables:
        if _TABLE in tables:
            raise RuntimeError(
                f"{_TABLE}가 core lessons 없이 존재합니다. partial schema를 자동 채택하지 않습니다."
            )
        return
    if _TABLE in tables:
        columns = {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}
        if columns != _REQUIRED_COLUMNS:
            missing = sorted(_REQUIRED_COLUMNS - columns)
            raise RuntimeError(
                f"{_TABLE} 부분 schema를 자동 보정하지 않습니다: missing={','.join(missing)}"
            )
        return

    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("object_path", sa.String(length=1024), nullable=False),
        sa.Column("folder_id", sa.String(length=12), nullable=False),
        sa.Column("filename", sa.String(length=64), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="staged"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.UniqueConstraint("object_path", name="uq_lesson_image_assets_object_path"),
        sa.UniqueConstraint("folder_id", "filename", name="uq_lesson_image_assets_location"),
        sa.CheckConstraint(
            "state IN ('staged','attached','orphan_candidate','cleanup_pending','deleted')",
            name="ck_lesson_image_assets_state",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_lesson_image_assets_attempt_nonnegative"),
    )
    op.create_index(
        "ix_lesson_image_assets_uploaded_by",
        _TABLE,
        ["uploaded_by"],
    )
    op.create_index(
        "ix_lesson_image_assets_state_cleanup",
        _TABLE,
        ["state", "cleanup_after"],
    )
    op.create_index(
        "ix_lesson_image_assets_lease",
        _TABLE,
        ["state", "lease_until"],
    )
    op.create_index(
        "ix_lesson_image_assets_cleanup_after",
        _TABLE,
        ["cleanup_after"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in _table_names(bind):
        return
    count = bind.execute(sa.text(f'SELECT count(*) FROM "{_TABLE}"')).scalar_one()
    if count:
        raise RuntimeError("lesson image lifecycle rows exist; archive/verify before downgrade")
    for name in (
        "ix_lesson_image_assets_cleanup_after",
        "ix_lesson_image_assets_lease",
        "ix_lesson_image_assets_state_cleanup",
        "ix_lesson_image_assets_uploaded_by",
    ):
        op.drop_index(name, table_name=_TABLE)
    op.drop_table(_TABLE)
