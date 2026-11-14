"""Add encrypted storage metadata for new challenge authoring flags.

Existing rows are not rewritten or decrypted by this migration. They remain
``legacy-raw`` until an operator performs a separately approved retention or
encryption procedure with backup and rollback evidence.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260815_024"
down_revision: Union[str, None] = "20260814_023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "challenge_submissions"
_CIPHERTEXT = "flag_ciphertext"
_KEY_VERSION = "flag_key_version"
_STORAGE_VERSION = "flag_storage_version"
_LENGTH = "flag_value_length"
_CHECK = "ck_challenge_submissions_flag_value_length_nonnegative"
_LEGACY_VERSION = "legacy-raw"


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _columns() -> set[str]:
    return {column["name"] for column in _inspector().get_columns(_TABLE)}


def _checks() -> set[str]:
    return {
        constraint.get("name")
        for constraint in _inspector().get_check_constraints(_TABLE)
        if constraint.get("name")
    }


def _require_table() -> None:
    if _TABLE not in set(_inspector().get_table_names()):
        # This additive table belongs to the runtime legacy baseline. A clean
        # database is bootstrapped by the explicit app wrapper, not here.
        raise RuntimeError(
            "challenge_submissions 테이블이 없어 플래그 암호화 메타데이터를 자동 생성하지 않습니다."
        )


def upgrade() -> None:
    _require_table()
    columns = _columns()

    if _CIPHERTEXT not in columns:
        op.add_column(_TABLE, sa.Column(_CIPHERTEXT, sa.LargeBinary(), nullable=True))
    if _KEY_VERSION not in columns:
        op.add_column(_TABLE, sa.Column(_KEY_VERSION, sa.String(length=64), nullable=True))
    if _STORAGE_VERSION not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                _STORAGE_VERSION,
                sa.String(length=40),
                nullable=False,
                server_default=_LEGACY_VERSION,
            ),
        )
        op.alter_column(_TABLE, _STORAGE_VERSION, server_default=None)
    if _LENGTH not in columns:
        op.add_column(
            _TABLE,
            sa.Column(_LENGTH, sa.Integer(), nullable=False, server_default="0"),
        )
        op.execute(
            sa.text(
                f"UPDATE {_TABLE} SET {_LENGTH} = char_length(flag_value) "
                f"WHERE {_LENGTH} = 0 AND flag_value IS NOT NULL"
            )
        )
        op.alter_column(_TABLE, _LENGTH, server_default=None)
    if _CHECK not in _checks():
        op.create_check_constraint(_CHECK, _TABLE, f"{_LENGTH} >= 0")


def downgrade() -> None:
    _require_table()
    columns = _columns()
    expected = {_CIPHERTEXT, _KEY_VERSION, _STORAGE_VERSION, _LENGTH}
    present = expected.intersection(columns)
    if not present:
        return
    if present != expected:
        raise RuntimeError(
            "문제 등록 플래그 암호화 메타데이터가 부분 적용되어 downgrade를 중단합니다."
        )

    bind = op.get_bind()
    non_legacy = bind.execute(
        sa.text(
            f"SELECT COUNT(*) FROM {_TABLE} WHERE "
            f"{_STORAGE_VERSION} <> :legacy OR {_CIPHERTEXT} IS NOT NULL "
            f"OR {_KEY_VERSION} IS NOT NULL"
        ),
        {"legacy": _LEGACY_VERSION},
    ).scalar_one()
    if non_legacy:
        raise RuntimeError(
            "암호화된 신규 문제 등록 행이 존재하여 destructive downgrade를 중단합니다."
        )

    if _CHECK in _checks():
        op.drop_constraint(_CHECK, _TABLE, type="check")
    for column in (_LENGTH, _STORAGE_VERSION, _KEY_VERSION, _CIPHERTEXT):
        op.drop_column(_TABLE, column)
