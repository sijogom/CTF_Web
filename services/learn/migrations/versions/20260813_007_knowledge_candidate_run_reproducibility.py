"""지식 관계 후보 실행의 재현성·건너뜀 원장을 보강한다.

006에서 만든 후보 실행 테이블의 기존 행은 보존한다. 새 allowlist input snapshot,
실행 상한, 제한 전 후보 수, 미리보기 절단 여부와 proposal 없이 건너뛴 후보의
identity snapshot을 추가한다. 이 migration은 콘텐츠·태그·관계·제안서를 생성,
삭제, 공개하지 않는다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260813_007"
down_revision: Union[str, None] = "20260813_006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _check_names(table_name: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if constraint["name"]
    }


def _require_v2_tables() -> None:
    tables = _table_names()
    required = {"knowledge_candidate_runs", "knowledge_candidate_run_items"}
    missing = sorted(required - tables)
    if missing:
        raise RuntimeError(
            "지식 후보 실행 재현성 migration의 기반 테이블이 없습니다: "
            + ", ".join(missing)
            + ". 20260813_006 적용 상태와 백업을 먼저 확인하세요."
        )


def _add_run_columns() -> None:
    columns = _column_names("knowledge_candidate_runs")
    if "execution_options" not in columns:
        op.add_column(
            "knowledge_candidate_runs",
            sa.Column(
                "execution_options",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )
    if "input_snapshot" not in columns:
        op.add_column(
            "knowledge_candidate_runs",
            sa.Column(
                "input_snapshot",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )
    if "candidate_count_before_limit" not in columns:
        op.add_column(
            "knowledge_candidate_runs",
            sa.Column("candidate_count_before_limit", sa.Integer(), nullable=False, server_default="0"),
        )
    if "preview_truncated" not in columns:
        op.add_column(
            "knowledge_candidate_runs",
            sa.Column("preview_truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def _replace_run_counts_check(*, expanded: bool) -> None:
    constraint_name = "ck_knowledge_candidate_run_counts"
    if constraint_name in _check_names("knowledge_candidate_runs"):
        op.drop_constraint(constraint_name, "knowledge_candidate_runs", type_="check")
    expression = (
        "input_count >= 0 AND candidate_count_before_limit >= 0 AND output_count >= 0 "
        "AND duplicate_count >= 0 AND error_count >= 0"
        if expanded
        else "input_count >= 0 AND output_count >= 0 AND duplicate_count >= 0 AND error_count >= 0"
    )
    op.create_check_constraint(constraint_name, "knowledge_candidate_runs", expression)


def _upgrade_run_item_contract() -> None:
    columns = _column_names("knowledge_candidate_run_items")
    if "candidate_snapshot" not in columns:
        op.add_column(
            "knowledge_candidate_run_items",
            sa.Column(
                "candidate_snapshot",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )
    op.alter_column(
        "knowledge_candidate_run_items",
        "proposal_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    constraint_name = "ck_knowledge_candidate_run_item_dedupe"
    if constraint_name in _check_names("knowledge_candidate_run_items"):
        op.drop_constraint(constraint_name, "knowledge_candidate_run_items", type_="check")
    op.create_check_constraint(
        constraint_name,
        "knowledge_candidate_run_items",
        "dedupe_outcome IN ('created', 'existing_skipped', 'rejected_precheck', 'preview_only')",
    )


def upgrade() -> None:
    _require_v2_tables()
    _add_run_columns()
    _replace_run_counts_check(expanded=True)
    _upgrade_run_item_contract()


def downgrade() -> None:
    """새 원장이 있으면 destructive downgrade 대신 fail-closed로 중단한다."""
    _require_v2_tables()
    bind = op.get_bind()
    run_data_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM knowledge_candidate_runs "
            "WHERE execution_options <> '{}'::jsonb OR input_snapshot <> '[]'::jsonb "
            "OR candidate_count_before_limit <> 0 OR preview_truncated IS TRUE"
        )
    ).scalar_one()
    item_data_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM knowledge_candidate_run_items "
            "WHERE proposal_id IS NULL OR candidate_snapshot <> '{}'::jsonb "
            "OR dedupe_outcome = 'preview_only'"
        )
    ).scalar_one()
    if run_data_count or item_data_count:
        raise RuntimeError(
            "지식 후보 실행 재현성 데이터가 있어 destructive downgrade를 중단합니다. "
            "검증된 백업 복구 또는 명시적 데이터 보존 계획을 사용하세요."
        )

    constraint_name = "ck_knowledge_candidate_run_item_dedupe"
    if constraint_name in _check_names("knowledge_candidate_run_items"):
        op.drop_constraint(constraint_name, "knowledge_candidate_run_items", type_="check")
    op.create_check_constraint(
        constraint_name,
        "knowledge_candidate_run_items",
        "dedupe_outcome IN ('created', 'existing_skipped', 'rejected_precheck')",
    )
    op.alter_column(
        "knowledge_candidate_run_items",
        "proposal_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    if "candidate_snapshot" in _column_names("knowledge_candidate_run_items"):
        op.drop_column("knowledge_candidate_run_items", "candidate_snapshot")

    _replace_run_counts_check(expanded=False)
    for column_name in (
        "preview_truncated",
        "candidate_count_before_limit",
        "input_snapshot",
        "execution_options",
    ):
        if column_name in _column_names("knowledge_candidate_runs"):
            op.drop_column("knowledge_candidate_runs", column_name)
