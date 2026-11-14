"""문제 이미지 cleanup outbox의 ORM 단일 lease 인덱스를 보정한다."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260816_027"
down_revision: Union[str, None] = "20260816_026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "challenge_image_cleanup_jobs"
_INDEX = "ix_challenge_image_cleanup_jobs_lease_until"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if _TABLE not in tables:
        raise RuntimeError(
            "challenge_image_cleanup_jobs가 없어 lease index migration을 적용할 수 없습니다."
        )
    indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if _INDEX not in indexes:
        op.create_index(_INDEX, _TABLE, ["lease_until"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return
    indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name=_TABLE)
