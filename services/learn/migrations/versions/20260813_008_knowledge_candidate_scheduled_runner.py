"""기본 비활성 scheduled 후보 runner의 원장/감사 필드를 추가한다.

기존 수동 run과 검토 이력은 보존한다. scheduled run은 존재하지 않는 DB 사용자 UUID를
만들지 않고 ``requested_by``/제출 review의 ``reviewer_id``를 NULL로 남긴다. 대신
trigger와 실행 주체를 명시하며, 동일 설정/snapshot은 하나의 deduplication key로
재사용한다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260813_008"
down_revision: Union[str, None] = "20260813_007"
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


def _index_names(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name) if index["name"]}


def _require_tables() -> None:
    required = {
        "knowledge_candidate_runs",
        "knowledge_relation_proposal_reviews",
    }
    missing = sorted(required - _table_names())
    if missing:
        raise RuntimeError(
            "scheduled 후보 runner migration의 기반 테이블이 없습니다: "
            + ", ".join(missing)
            + ". 20260813_006 및 20260813_007 적용 상태를 먼저 확인하세요."
        )


def _add_run_columns() -> None:
    columns = _column_names("knowledge_candidate_runs")
    if "trigger_kind" not in columns:
        op.add_column(
            "knowledge_candidate_runs",
            sa.Column(
                "trigger_kind",
                sa.String(length=24),
                nullable=False,
                server_default=sa.text("'manual'"),
            ),
        )
    if "execution_principal" not in columns:
        op.add_column(
            "knowledge_candidate_runs",
            sa.Column(
                "execution_principal",
                sa.String(length=100),
                nullable=False,
                server_default=sa.text("'admin_api'"),
            ),
        )
    if "deduplication_key" not in columns:
        op.add_column(
            "knowledge_candidate_runs",
            sa.Column("deduplication_key", sa.String(length=64), nullable=True),
        )
    if "attempt_count" not in columns:
        op.add_column(
            "knowledge_candidate_runs",
            sa.Column(
                "attempt_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )


def _add_run_constraints_and_indexes() -> None:
    check_names = _check_names("knowledge_candidate_runs")
    if "ck_knowledge_candidate_run_trigger_kind" not in check_names:
        op.create_check_constraint(
            "ck_knowledge_candidate_run_trigger_kind",
            "knowledge_candidate_runs",
            "trigger_kind IN ('manual', 'scheduled')",
        )
    if "ck_knowledge_candidate_run_attempt_count" not in check_names:
        op.create_check_constraint(
            "ck_knowledge_candidate_run_attempt_count",
            "knowledge_candidate_runs",
            "attempt_count >= 1",
        )

    index_names = _index_names("knowledge_candidate_runs")
    if "ix_knowledge_candidate_runs_trigger_created" not in index_names:
        op.create_index(
            "ix_knowledge_candidate_runs_trigger_created",
            "knowledge_candidate_runs",
            ["trigger_kind", "created_at"],
        )
    if "uq_knowledge_candidate_runs_deduplication_key" not in index_names:
        op.create_index(
            "uq_knowledge_candidate_runs_deduplication_key",
            "knowledge_candidate_runs",
            ["deduplication_key"],
            unique=True,
            postgresql_where=sa.text("deduplication_key IS NOT NULL"),
        )


def upgrade() -> None:
    _require_tables()
    _add_run_columns()
    op.alter_column(
        "knowledge_candidate_runs",
        "requested_by",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.alter_column(
        "knowledge_relation_proposal_reviews",
        "reviewer_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    _add_run_constraints_and_indexes()


def downgrade() -> None:
    """scheduled 감사 데이터가 있으면 destructive downgrade를 fail-closed로 막는다."""
    _require_tables()
    run_columns = _column_names("knowledge_candidate_runs")
    review_columns = _column_names("knowledge_relation_proposal_reviews")
    if not {"trigger_kind", "execution_principal", "deduplication_key", "attempt_count"}.issubset(run_columns):
        return

    bind = op.get_bind()
    run_data_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM knowledge_candidate_runs "
            "WHERE trigger_kind <> 'manual' OR execution_principal <> 'admin_api' "
            "OR deduplication_key IS NOT NULL OR attempt_count <> 1 OR requested_by IS NULL"
        )
    ).scalar_one()
    system_review_count = 0
    if "reviewer_id" in review_columns:
        system_review_count = bind.execute(
            sa.text("SELECT COUNT(*) FROM knowledge_relation_proposal_reviews WHERE reviewer_id IS NULL")
        ).scalar_one()
    if run_data_count or system_review_count:
        raise RuntimeError(
            "scheduled 후보 runner 감사 데이터가 있어 destructive downgrade를 중단합니다. "
            "검증된 백업 복원 또는 명시적 데이터 보존 계획을 사용하세요."
        )

    index_names = _index_names("knowledge_candidate_runs")
    if "uq_knowledge_candidate_runs_deduplication_key" in index_names:
        op.drop_index("uq_knowledge_candidate_runs_deduplication_key", table_name="knowledge_candidate_runs")
    if "ix_knowledge_candidate_runs_trigger_created" in index_names:
        op.drop_index("ix_knowledge_candidate_runs_trigger_created", table_name="knowledge_candidate_runs")

    check_names = _check_names("knowledge_candidate_runs")
    for constraint_name in (
        "ck_knowledge_candidate_run_attempt_count",
        "ck_knowledge_candidate_run_trigger_kind",
    ):
        if constraint_name in check_names:
            op.drop_constraint(constraint_name, "knowledge_candidate_runs", type_="check")

    op.alter_column(
        "knowledge_relation_proposal_reviews",
        "reviewer_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.alter_column(
        "knowledge_candidate_runs",
        "requested_by",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    for column_name in (
        "attempt_count",
        "deduplication_key",
        "execution_principal",
        "trigger_kind",
    ):
        if column_name in run_columns:
            op.drop_column("knowledge_candidate_runs", column_name)
