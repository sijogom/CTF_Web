"""Move Course soft-delete DDL out of the Learn runtime worker.

The previous application lifespan added these columns with ``IF NOT EXISTS``.
That could silently repair only part of a damaged database whenever a worker
started.  This revision is the only versioned upgrade path for existing
versioned Learn databases; pristine databases receive the same final contract
from the explicit bootstrap job and are stamped at head.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260813_011"
down_revision: Union[str, None] = "20260813_010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "courses"
_COLUMNS = {
    "is_deleted",
    "deleted_at",
    "deleted_by",
    "deletion_reason",
    "published_before_deletion",
}
_INDEX = "ix_courses_is_deleted"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _index_names() -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
        if index.get("name")
    }


def _require_course_table() -> None:
    if _TABLE not in _table_names():
        raise RuntimeError(
            "Course soft-delete migration은 courses 테이블이 없는 DB를 자동 초기화하지 않습니다. "
            "Learn bootstrap 또는 검증된 백업 복구 절차를 사용하세요."
        )


def _add_boolean_column(column_name: str) -> None:
    """Add a populated non-null boolean without leaving a server default behind."""
    op.add_column(
        _TABLE,
        sa.Column(column_name, sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column(_TABLE, column_name, server_default=None)


def upgrade() -> None:
    _require_course_table()
    columns = _column_names()
    if "is_deleted" not in columns:
        _add_boolean_column("is_deleted")
    if "deleted_at" not in columns:
        op.add_column(_TABLE, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    if "deleted_by" not in columns:
        op.add_column(_TABLE, sa.Column("deleted_by", postgresql.UUID(as_uuid=True), nullable=True))
    if "deletion_reason" not in columns:
        op.add_column(_TABLE, sa.Column("deletion_reason", sa.Text(), nullable=True))
    if "published_before_deletion" not in columns:
        _add_boolean_column("published_before_deletion")
    if _INDEX not in _index_names():
        op.create_index(_INDEX, _TABLE, ["is_deleted"])


def downgrade() -> None:
    _require_course_table()
    columns = _column_names()
    present = _COLUMNS.intersection(columns)
    if not present:
        return
    if present != _COLUMNS:
        raise RuntimeError(
            "Course soft-delete schema가 부분 상태여서 자동 downgrade를 중단합니다."
        )

    count = op.get_bind().execute(
        sa.text(
            "SELECT COUNT(*) FROM courses "
            "WHERE is_deleted IS TRUE OR deleted_at IS NOT NULL OR deleted_by IS NOT NULL "
            "OR deletion_reason IS NOT NULL OR published_before_deletion IS TRUE"
        )
    ).scalar_one()
    if count:
        raise RuntimeError(
            "Course soft-delete 이력 데이터가 있어 destructive downgrade를 중단합니다. "
            "검증된 백업 복구 또는 명시적 보존 계획을 사용하세요."
        )

    if _INDEX in _index_names():
        op.drop_index(_INDEX, table_name=_TABLE)
    for column_name in (
        "published_before_deletion",
        "deletion_reason",
        "deleted_by",
        "deleted_at",
        "is_deleted",
    ):
        op.drop_column(_TABLE, column_name)
