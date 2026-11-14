"""Add irreversible-submission metadata without rewriting legacy rows.

New submissions keep only a fixed marker, length, and optional keyed digest.  Existing
rows are deliberately left untouched so an operator can choose a separately reviewed
retention/encryption migration with a backup and rollback plan.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260814_013"
down_revision: Union[str, None] = "20260813_012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "submissions"
_LENGTH = "submitted_flag_length"
_FINGERPRINT = "submitted_flag_fingerprint"
_VERSION = "submitted_flag_storage_version"
_CHECK = "ck_submissions_submitted_flag_length_nonnegative"
_LEGACY_VERSION = "legacy-raw"


def _inspect() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _columns() -> set[str]:
    return {column["name"] for column in _inspect().get_columns(_TABLE)}


def _checks() -> set[str]:
    return {
        check["name"]
        for check in _inspect().get_check_constraints(_TABLE)
        if check.get("name")
    }


def _require_table() -> None:
    if _TABLE not in set(_inspect().get_table_names()):
        raise RuntimeError(
            "submissions 테이블이 없어 제출 메타데이터 migration을 자동 초기화하지 않습니다."
        )


def upgrade() -> None:
    _require_table()
    columns = _columns()

    if _LENGTH not in columns:
        op.add_column(
            _TABLE,
            sa.Column(_LENGTH, sa.Integer(), nullable=False, server_default="0"),
        )
        op.execute(
            sa.text(
                f"UPDATE {_TABLE} SET {_LENGTH} = char_length(submitted_flag) "
                f"WHERE {_LENGTH} = 0 AND submitted_flag IS NOT NULL"
            )
        )
        op.alter_column(_TABLE, _LENGTH, server_default=None)

    if _FINGERPRINT not in columns:
        op.add_column(
            _TABLE,
            sa.Column(_FINGERPRINT, sa.LargeBinary(length=32), nullable=True),
        )

    if _VERSION not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                _VERSION,
                sa.String(length=40),
                nullable=False,
                server_default=_LEGACY_VERSION,
            ),
        )
        op.alter_column(_TABLE, _VERSION, server_default=None)

    if _CHECK not in _checks():
        op.create_check_constraint(
            _CHECK,
            _TABLE,
            f"{_LENGTH} >= 0",
        )


def downgrade() -> None:
    _require_table()
    columns = _columns()
    present = {_LENGTH, _FINGERPRINT, _VERSION}.intersection(columns)
    if not present:
        return
    if present != {_LENGTH, _FINGERPRINT, _VERSION}:
        raise RuntimeError(
            "제출 redaction metadata가 부분 적용되어 destructive downgrade를 중단합니다."
        )

    bind = op.get_bind()
    non_legacy = bind.execute(
        sa.text(
            f"SELECT COUNT(*) FROM {_TABLE} WHERE "
            f"{_VERSION} <> :legacy OR {_FINGERPRINT} IS NOT NULL "
            f"OR {_LENGTH} <> char_length(submitted_flag)"
        ),
        {"legacy": _LEGACY_VERSION},
    ).scalar_one()
    if non_legacy:
        raise RuntimeError(
            "신규 제출 redaction metadata가 존재하여 destructive downgrade를 중단합니다."
        )

    if _CHECK in _checks():
        op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, _VERSION)
    op.drop_column(_TABLE, _FINGERPRINT)
    op.drop_column(_TABLE, _LENGTH)
