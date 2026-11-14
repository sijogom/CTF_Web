"""지식 매핑 V2의 후보·검토·안전 프로필·Outbox 원장을 추가한다.

기존 ``knowledge_content_nodes``, ``knowledge_relations``,
``knowledge_relation_reviews``는 보존한다. 이 migration은 기존 공개 관계를 후보로
자동 변환하거나, 선행조건·잠금·원본 콘텐츠·태그를 수정하지 않는다. V2 후보는
승인 전에는 public graph가 읽지 않는 별도 테이블에만 저장된다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260813_006"
# Alembic depends on the predecessor's ``revision`` value, not its filename.
# The dynamic flag migration file is named ``..._dynamic_instance_flags.py`` but
# declares revision ``20260813_005``.
down_revision: Union[str, None] = "20260813_005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _require_columns(table_name: str, expected: set[str]) -> None:
    actual = _column_names(table_name)
    missing = sorted(expected - actual)
    if missing:
        raise RuntimeError(
            f"기존 {table_name} 테이블이 지식 매핑 V2 계약과 다릅니다. "
            f"누락 컬럼: {', '.join(missing)}. 백업과 별도 복구 migration을 먼저 확인하세요."
        )


def _create_index_if_missing(
    table_name: str,
    index_name: str,
    columns: list[str],
    *,
    unique: bool = False,
    postgresql_where=None,
) -> None:
    if index_name not in _index_names(table_name):
        op.create_index(
            index_name,
            table_name,
            columns,
            unique=unique,
            postgresql_where=postgresql_where,
        )


def _ensure_relation_type_contract() -> None:
    """V2의 ``prepares_for``만 기존 공개 관계 어휘에 추가한다.

    PostgreSQL CHECK 제약 이름은 001 migration에서 고정되어 있으므로, 기존 행을
    수정하지 않고 제약만 교체한다. 실패 시 전체 transaction이 rollback되어 새
    어휘가 반쯤 적용되지 않는다.
    """
    table_names = _table_names()
    if "knowledge_relations" not in table_names:
        raise RuntimeError("knowledge_relations가 없어 V2 관계 어휘를 확장할 수 없습니다.")
    check_constraints = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints("knowledge_relations")
    }
    constraint_name = "ck_knowledge_relation_type"
    if constraint_name in check_constraints:
        op.drop_constraint(constraint_name, "knowledge_relations", type_="check")
    op.create_check_constraint(
        constraint_name,
        "knowledge_relations",
        "relation_type IN ('requires', 'prepares_for', 'recommends', 'helps', 'related', 'similar', 'tag_affinity')",
    )


def _create_content_profiles() -> None:
    op.create_table(
        "knowledge_content_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_revision", sa.String(length=64), nullable=False),
        sa.Column("profile_schema_version", sa.String(length=40), nullable=False),
        sa.Column(
            "allowed_profile",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("profile_digest", sa.String(length=64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["knowledge_content_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id", "content_revision", name="uq_knowledge_content_profile_revision"),
    )


def _create_candidate_runs() -> None:
    op.create_table(
        "knowledge_candidate_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_kind", sa.String(length=24), nullable=False),
        sa.Column("algorithm_version", sa.String(length=120), nullable=False),
        sa.Column("configuration_digest", sa.String(length=64), nullable=False),
        sa.Column("input_snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("is_dry_run", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_count", sa.Integer(), nullable=False),
        sa.Column("output_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("run_kind IN ('rule', 'model', 'import')", name="ck_knowledge_candidate_run_kind"),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_knowledge_candidate_run_state",
        ),
        sa.CheckConstraint(
            "input_count >= 0 AND output_count >= 0 AND duplicate_count >= 0 AND error_count >= 0",
            name="ck_knowledge_candidate_run_counts",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_proposals() -> None:
    op.create_table(
        "knowledge_relation_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("base_relation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("published_relation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("relation_type", sa.String(length=24), nullable=False),
        sa.Column("is_directed", sa.Boolean(), nullable=False),
        sa.Column("strength", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("origin", sa.String(length=24), nullable=False),
        sa.Column("evidence_summary", sa.Text(), nullable=True),
        sa.Column("quality_version", sa.String(length=120), nullable=True),
        sa.Column("generation_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("content_revision", sa.String(length=64), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source_node_id <> target_node_id", name="ck_knowledge_relation_proposal_no_self_ref"),
        sa.CheckConstraint(
            "relation_type IN ('requires', 'prepares_for', 'recommends', 'helps', 'related', 'similar', 'tag_affinity')",
            name="ck_knowledge_relation_proposal_type",
        ),
        sa.CheckConstraint("strength >= 0 AND strength <= 1", name="ck_knowledge_relation_proposal_strength"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_knowledge_relation_proposal_confidence"),
        sa.CheckConstraint(
            "origin IN ('manual', 'rule', 'model_candidate', 'import', 'observation')",
            name="ck_knowledge_relation_proposal_origin",
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'pending_review', 'approved', 'rejected', 'retired', 'superseded')",
            name="ck_knowledge_relation_proposal_state",
        ),
        sa.ForeignKeyConstraint(["base_relation_id"], ["knowledge_relations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["published_relation_id"], ["knowledge_relations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_node_id"], ["knowledge_content_nodes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_node_id"], ["knowledge_content_nodes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_profile_id"], ["knowledge_content_profiles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["target_profile_id"], ["knowledge_content_profiles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["generation_run_id"], ["knowledge_candidate_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_proposal_evidence() -> None:
    op.create_table(
        "knowledge_relation_proposal_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_kind", sa.String(length=24), nullable=False),
        sa.Column("source_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_digest", sa.String(length=64), nullable=True),
        sa.Column("locator", sa.String(length=500), nullable=True),
        sa.Column("summary", sa.String(length=1000), nullable=False),
        sa.Column("score_component", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("generator_version", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "evidence_kind IN ('manual', 'rule', 'model', 'metadata', 'reference')",
            name="ck_knowledge_relation_proposal_evidence_kind",
        ),
        sa.ForeignKeyConstraint(["proposal_id"], ["knowledge_relation_proposals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_node_id"], ["knowledge_content_nodes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_proposal_reviews() -> None:
    op.create_table(
        "knowledge_relation_proposal_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("before_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('submitted', 'approved', 'rejected', 'retired', 'superseded')",
            name="ck_knowledge_relation_proposal_review_action",
        ),
        sa.ForeignKeyConstraint(["proposal_id"], ["knowledge_relation_proposals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_candidate_run_items() -> None:
    op.create_table(
        "knowledge_candidate_run_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("raw_score", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("dedupe_outcome", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rank >= 0", name="ck_knowledge_candidate_run_item_rank"),
        sa.CheckConstraint("raw_score >= 0 AND raw_score <= 1", name="ck_knowledge_candidate_run_item_score"),
        sa.CheckConstraint(
            "dedupe_outcome IN ('created', 'existing_skipped', 'rejected_precheck')",
            name="ck_knowledge_candidate_run_item_dedupe",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["knowledge_candidate_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["proposal_id"], ["knowledge_relation_proposals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "proposal_id", name="uq_knowledge_candidate_run_item"),
    )


def _create_projection_outbox() -> None:
    op.create_table(
        "knowledge_projection_outbox",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("aggregate_type", sa.String(length=40), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("graph_revision", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "aggregate_type IN ('knowledge_relation', 'knowledge_relation_proposal')",
            name="ck_knowledge_projection_outbox_aggregate",
        ),
        sa.CheckConstraint(
            "event_type IN ('proposal_approved', 'proposal_rejected', 'proposal_retired', "
            "'relation_published', 'relation_retired')",
            name="ck_knowledge_projection_outbox_event",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def upgrade() -> None:
    tables = _table_names()
    _require_columns(
        "knowledge_content_nodes",
        {"id", "entity_type", "entity_id", "display_name", "is_active", "source_updated_at"},
    )
    _require_columns(
        "knowledge_relations",
        {
            "id", "source_node_id", "target_node_id", "relation_type", "is_directed",
            "strength", "confidence", "origin", "evidence_summary", "status", "created_by",
        },
    )
    _ensure_relation_type_contract()

    if "knowledge_content_profiles" not in tables:
        _create_content_profiles()
    else:
        _require_columns(
            "knowledge_content_profiles",
            {"id", "node_id", "content_revision", "profile_schema_version", "allowed_profile", "profile_digest", "generated_at"},
        )
    if "knowledge_candidate_runs" not in tables:
        _create_candidate_runs()
    else:
        _require_columns(
            "knowledge_candidate_runs",
            {"id", "run_kind", "algorithm_version", "configuration_digest", "input_snapshot_digest", "requested_by", "state", "is_dry_run", "created_at"},
        )
    if "knowledge_relation_proposals" not in tables:
        _create_proposals()
    else:
        _require_columns(
            "knowledge_relation_proposals",
            {"id", "base_relation_id", "published_relation_id", "source_node_id", "target_node_id", "relation_type", "state", "content_revision", "created_at"},
        )
    if "knowledge_relation_proposal_evidence" not in tables:
        _create_proposal_evidence()
    else:
        _require_columns(
            "knowledge_relation_proposal_evidence",
            {"id", "proposal_id", "evidence_kind", "summary", "created_at"},
        )
    if "knowledge_relation_proposal_reviews" not in tables:
        _create_proposal_reviews()
    else:
        _require_columns(
            "knowledge_relation_proposal_reviews",
            {"id", "proposal_id", "reviewer_id", "action", "created_at"},
        )
    if "knowledge_candidate_run_items" not in tables:
        _create_candidate_run_items()
    else:
        _require_columns(
            "knowledge_candidate_run_items",
            {"id", "run_id", "proposal_id", "rank", "raw_score", "dedupe_outcome", "created_at"},
        )
    if "knowledge_projection_outbox" not in tables:
        _create_projection_outbox()
    else:
        _require_columns(
            "knowledge_projection_outbox",
            {"id", "aggregate_type", "aggregate_id", "event_type", "graph_revision", "payload", "payload_digest", "created_at", "processed_at"},
        )

    _create_index_if_missing("knowledge_content_profiles", "ix_knowledge_content_profiles_node_id", ["node_id"])
    _create_index_if_missing("knowledge_content_profiles", "ix_knowledge_content_profiles_node_generated", ["node_id", "generated_at"])
    _create_index_if_missing("knowledge_candidate_runs", "ix_knowledge_candidate_runs_requested_by", ["requested_by"])
    _create_index_if_missing("knowledge_candidate_runs", "ix_knowledge_candidate_runs_state", ["state"])
    _create_index_if_missing("knowledge_candidate_runs", "ix_knowledge_candidate_runs_state_created", ["state", "created_at"])
    _create_index_if_missing("knowledge_candidate_runs", "ix_knowledge_candidate_runs_requested_created", ["requested_by", "created_at"])
    _create_index_if_missing("knowledge_relation_proposals", "ix_knowledge_relation_proposals_base_relation_id", ["base_relation_id"])
    _create_index_if_missing("knowledge_relation_proposals", "ix_knowledge_relation_proposals_published_relation_id", ["published_relation_id"])
    _create_index_if_missing("knowledge_relation_proposals", "ix_knowledge_relation_proposals_generation_run_id", ["generation_run_id"])
    _create_index_if_missing("knowledge_relation_proposals", "ix_knowledge_relation_proposals_created_by", ["created_by"])
    _create_index_if_missing("knowledge_relation_proposals", "ix_knowledge_relation_proposals_reviewed_by", ["reviewed_by"])
    _create_index_if_missing("knowledge_relation_proposals", "ix_knowledge_relation_proposals_state", ["state"])
    _create_index_if_missing("knowledge_relation_proposals", "ix_knowledge_relation_proposals_state_created", ["state", "created_at"])
    _create_index_if_missing("knowledge_relation_proposals", "ix_knowledge_relation_proposals_source_state", ["source_node_id", "state"])
    _create_index_if_missing("knowledge_relation_proposals", "ix_knowledge_relation_proposals_target_state", ["target_node_id", "state"])
    _create_index_if_missing(
        "knowledge_relation_proposals",
        "uq_knowledge_relation_proposal_active_pair",
        ["source_node_id", "target_node_id", "relation_type", "is_directed"],
        unique=True,
        postgresql_where=sa.text("state IN ('draft', 'pending_review')"),
    )
    _create_index_if_missing("knowledge_relation_proposal_evidence", "ix_knowledge_relation_proposal_evidence_proposal_created", ["proposal_id", "created_at"])
    _create_index_if_missing("knowledge_relation_proposal_evidence", "ix_knowledge_relation_proposal_evidence_source_node_id", ["source_node_id"])
    _create_index_if_missing("knowledge_relation_proposal_reviews", "ix_knowledge_relation_proposal_reviews_proposal_created", ["proposal_id", "created_at"])
    _create_index_if_missing("knowledge_relation_proposal_reviews", "ix_knowledge_relation_proposal_reviews_reviewer_id", ["reviewer_id"])
    _create_index_if_missing("knowledge_candidate_run_items", "ix_knowledge_candidate_run_items_run_rank", ["run_id", "rank"])
    _create_index_if_missing("knowledge_candidate_run_items", "ix_knowledge_candidate_run_items_proposal_id", ["proposal_id"])
    _create_index_if_missing("knowledge_projection_outbox", "ix_knowledge_projection_outbox_graph_revision", ["graph_revision"])
    _create_index_if_missing("knowledge_projection_outbox", "ix_knowledge_projection_outbox_processed_created", ["processed_at", "created_at"])
    _create_index_if_missing("knowledge_projection_outbox", "ix_knowledge_projection_outbox_aggregate", ["aggregate_type", "aggregate_id", "created_at"])


def downgrade() -> None:
    """데이터가 있으면 파괴적 downgrade 대신 fail-closed로 중단한다."""
    bind = op.get_bind()
    tables = _table_names()
    v2_tables = [
        "knowledge_projection_outbox",
        "knowledge_candidate_run_items",
        "knowledge_relation_proposal_reviews",
        "knowledge_relation_proposal_evidence",
        "knowledge_relation_proposals",
        "knowledge_candidate_runs",
        "knowledge_content_profiles",
    ]
    populated = []
    for table_name in v2_tables:
        if table_name in tables:
            count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
            if count:
                populated.append(f"{table_name}={count}")
    if populated:
        raise RuntimeError(
            "지식 매핑 V2 데이터가 있어 destructive downgrade를 중단합니다: "
            + ", ".join(populated)
            + ". 기능을 비활성화하거나 검증된 백업 복구를 사용하세요."
        )

    # V2 public relation이 남아 있으면 기존 CHECK로 되돌리면 데이터가 사라지거나
    # 제약 적용이 실패할 수 있다. 관리자 은퇴/백업 복구 없이 자동 삭제하지 않는다.
    prepares_for_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM knowledge_relations WHERE relation_type = 'prepares_for'")
    ).scalar_one()
    if prepares_for_count:
        raise RuntimeError(
            "prepares_for 공개 관계가 있어 기존 관계 어휘로 downgrade할 수 없습니다. "
            "관계를 명시적으로 은퇴하거나 검증된 백업 복구를 사용하세요."
        )

    for table_name in v2_tables:
        if table_name in tables:
            op.drop_table(table_name)
    check_constraints = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints("knowledge_relations")
    }
    if "ck_knowledge_relation_type" in check_constraints:
        op.drop_constraint("ck_knowledge_relation_type", "knowledge_relations", type_="check")
    op.create_check_constraint(
        "ck_knowledge_relation_type",
        "knowledge_relations",
        "relation_type IN ('requires', 'recommends', 'helps', 'related', 'similar', 'tag_affinity')",
    )
