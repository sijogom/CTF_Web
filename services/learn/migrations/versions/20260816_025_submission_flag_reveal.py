"""Store submitted flags encrypted and audit restricted administrator reveals."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260816_025"
down_revision: Union[str, None] = "20260815_024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "submissions"
_AUDIT_TABLE = "submission_flag_reveal_audits"
_CIPHERTEXT = "submitted_flag_ciphertext"
_KEY_VERSION = "submitted_flag_key_version"


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    inspector = _inspector()
    tables = set(inspector.get_table_names())
    if _TABLE not in tables:
        raise RuntimeError(
            "submissions 테이블이 없어 제출 원문 암호화 계약을 자동 생성하지 않습니다."
        )

    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _CIPHERTEXT not in columns:
        op.add_column(_TABLE, sa.Column(_CIPHERTEXT, sa.LargeBinary(), nullable=True))
    if _KEY_VERSION not in columns:
        op.add_column(_TABLE, sa.Column(_KEY_VERSION, sa.String(length=64), nullable=True))

    if _AUDIT_TABLE not in tables:
        op.create_table(
            _AUDIT_TABLE,
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "viewed_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.ForeignKeyConstraint(
                ["submission_id"],
                [f"{_TABLE}.id"],
                name="fk_submission_flag_reveal_audits_submission",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_submission_flag_reveal_audits_submission_viewed",
            _AUDIT_TABLE,
            ["submission_id", "viewed_at"],
        )
        op.create_index(
            "ix_submission_flag_reveal_audits_actor_viewed",
            _AUDIT_TABLE,
            ["actor_id", "viewed_at"],
        )


def downgrade() -> None:
    inspector = _inspector()
    tables = set(inspector.get_table_names())
    if _AUDIT_TABLE in tables:
        count = op.get_bind().execute(
            sa.text(f"SELECT COUNT(*) FROM {_AUDIT_TABLE}")
        ).scalar_one()
        if count:
            raise RuntimeError(
                "제출 원문 열람 감사 기록이 있어 destructive downgrade를 중단합니다."
            )
        op.drop_index(
            "ix_submission_flag_reveal_audits_actor_viewed",
            table_name=_AUDIT_TABLE,
        )
        op.drop_index(
            "ix_submission_flag_reveal_audits_submission_viewed",
            table_name=_AUDIT_TABLE,
        )
        op.drop_table(_AUDIT_TABLE)

    if _TABLE not in tables:
        return
    columns = {column["name"] for column in _inspector().get_columns(_TABLE)}
    if _CIPHERTEXT in columns:
        non_empty = op.get_bind().execute(
            sa.text(
                f"SELECT COUNT(*) FROM {_TABLE} "
                f"WHERE {_CIPHERTEXT} IS NOT NULL OR {_KEY_VERSION} IS NOT NULL"
            )
        ).scalar_one()
        if non_empty:
            raise RuntimeError(
                "암호화된 제출 원문이 존재하여 destructive downgrade를 중단합니다."
            )
    if _KEY_VERSION in columns:
        op.drop_column(_TABLE, _KEY_VERSION)
    if _CIPHERTEXT in columns:
        op.drop_column(_TABLE, _CIPHERTEXT)
