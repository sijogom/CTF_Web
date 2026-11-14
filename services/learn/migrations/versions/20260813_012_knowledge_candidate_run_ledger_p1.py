"""Harden the reproducible knowledge-candidate execution ledger.

Existing run rows stay intact: their input snapshot format is explicitly marked
``legacy-unversioned`` rather than being rewritten or guessed.  New runs record
the allowlisted profile/node snapshot version and requested candidate cap in
first-class columns.  This migration creates no candidates, public relations,
locks, or graph projection events.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260813_012"
down_revision: Union[str, None] = "20260813_011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "knowledge_candidate_runs"
_SCHEMA_VERSION_COLUMN = "input_snapshot_schema_version"
_REQUESTED_MAX_COLUMN = "requested_max_candidates"
_REQUESTED_MAX_CHECK = "ck_knowledge_candidate_run_requested_max_candidates"
_LEGACY_SCHEMA_VERSION = "legacy-unversioned"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _check_names() -> set[str]:
    return {
        check["name"]
        for check in sa.inspect(op.get_bind()).get_check_constraints(_TABLE)
        if check.get("name")
    }


def _require_candidate_run_table() -> None:
    if _TABLE not in _table_names():
        raise RuntimeError(
            "Knowledge candidate ledger P1 migration은 knowledge_candidate_runs 테이블이 없는 "
            "DB를 자동 초기화하지 않습니다. 검증된 Learn bootstrap 또는 백업 복구 절차를 사용하세요."
        )


def upgrade() -> None:
    """Additive-only ledger metadata; old rows retain an honest legacy marker."""
    _require_candidate_run_table()
    columns = _column_names()
    if _SCHEMA_VERSION_COLUMN not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                _SCHEMA_VERSION_COLUMN,
                sa.String(length=40),
                nullable=False,
                # Keep the server default for a rolling deployment where an older API
                # process might write a row before it is replaced by the new image.
                server_default=sa.text(f"'{_LEGACY_SCHEMA_VERSION}'"),
            ),
        )
    if _REQUESTED_MAX_COLUMN not in columns:
        op.add_column(_TABLE, sa.Column(_REQUESTED_MAX_COLUMN, sa.Integer(), nullable=True))
    if _REQUESTED_MAX_CHECK not in _check_names():
        op.create_check_constraint(
            _REQUESTED_MAX_CHECK,
            _TABLE,
            "requested_max_candidates IS NULL OR "
            "(requested_max_candidates >= 1 AND requested_max_candidates <= 500)",
        )


def downgrade() -> None:
    """Refuse to discard P1 execution evidence or a partially applied contract."""
    _require_candidate_run_table()
    columns = _column_names()
    present = {_SCHEMA_VERSION_COLUMN, _REQUESTED_MAX_COLUMN}.intersection(columns)
    if not present:
        return
    if present != {_SCHEMA_VERSION_COLUMN, _REQUESTED_MAX_COLUMN}:
        raise RuntimeError(
            "Knowledge candidate ledger P1 schema가 부분 상태여서 destructive downgrade를 중단합니다."
        )
    if _REQUESTED_MAX_CHECK not in _check_names():
        raise RuntimeError(
            "Knowledge candidate ledger P1 constraint가 없어 부분 상태 downgrade를 중단합니다."
        )

    p1_data_count = op.get_bind().execute(
        sa.text(
            "SELECT COUNT(*) FROM knowledge_candidate_runs "
            "WHERE input_snapshot_schema_version IS NULL "
            "OR input_snapshot_schema_version <> :legacy_schema_version "
            "OR requested_max_candidates IS NOT NULL"
        ),
        {"legacy_schema_version": _LEGACY_SCHEMA_VERSION},
    ).scalar_one()
    if p1_data_count:
        raise RuntimeError(
            "profile/node snapshot 또는 요청 상한이 기록된 Knowledge candidate ledger P1 데이터가 있어 "
            "destructive downgrade를 중단합니다. 검증된 백업 복구 또는 명시적 보존 계획을 사용하세요."
        )

    op.drop_constraint(_REQUESTED_MAX_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, _REQUESTED_MAX_COLUMN)
    op.drop_column(_TABLE, _SCHEMA_VERSION_COLUMN)
