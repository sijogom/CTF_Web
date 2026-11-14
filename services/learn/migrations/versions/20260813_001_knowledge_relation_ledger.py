"""지식 관계 원장 테이블 추가.

기존 태그·선행조건·추천 데이터에는 DML을 하지 않는다. 과거 개발 기동으로 테이블이
이미 만들어진 경우에는 필요한 컬럼을 검증한 뒤 인덱스만 보완한다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260813_001"
down_revision: Union[str, None] = "20260813_000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _require_columns(table_name: str, expected: set[str]) -> None:
    actual = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }
    missing = sorted(expected - actual)
    if missing:
        raise RuntimeError(
            f"기존 {table_name} 테이블이 지식 관계 원장 계약과 다릅니다. "
            f"누락 컬럼: {', '.join(missing)}. 백업 후 별도 복구 migration을 적용하세요."
        )


def _create_index_if_missing(
    table_name: str,
    index_name: str,
    columns: list[str],
    *,
    unique: bool = False,
    postgresql_where=None,
) -> None:
    existing = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }
    if index_name not in existing:
        op.create_index(
            index_name,
            table_name,
            columns,
            unique=unique,
            postgresql_where=postgresql_where,
        )


def _create_content_nodes() -> None:
    op.create_table(
        "knowledge_content_nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(length=24), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_digest", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "entity_type IN ('challenge', 'course', 'lesson', 'knowledge_tag', 'hint')",
            name="ck_knowledge_content_node_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity_type", "entity_id", name="uq_knowledge_content_node_entity"),
    )


def _create_relations() -> None:
    op.create_table(
        "knowledge_relations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation_type", sa.String(length=24), nullable=False),
        sa.Column("is_directed", sa.Boolean(), nullable=False),
        sa.Column("strength", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("origin", sa.String(length=24), nullable=False),
        sa.Column("evidence_summary", sa.Text(), nullable=True),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("quality_version", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("legacy_reference", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source_node_id <> target_node_id", name="ck_knowledge_relation_no_self_ref"),
        sa.CheckConstraint(
            "relation_type IN ('requires', 'recommends', 'helps', 'related', 'similar', 'tag_affinity')",
            name="ck_knowledge_relation_type",
        ),
        sa.CheckConstraint("strength >= 0 AND strength <= 1", name="ck_knowledge_relation_strength"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_knowledge_relation_confidence"),
        sa.CheckConstraint(
            "origin IN ('manual', 'rule', 'model_candidate', 'import', 'observation')",
            name="ck_knowledge_relation_origin",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_review', 'approved', 'rejected', 'retired')",
            name="ck_knowledge_relation_status",
        ),
        sa.ForeignKeyConstraint(["source_node_id"], ["knowledge_content_nodes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_node_id"], ["knowledge_content_nodes.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_relation_reviews() -> None:
    op.create_table(
        "knowledge_relation_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("before_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('submitted', 'edited', 'approved', 'rejected', 'retired')",
            name="ck_knowledge_relation_review_action",
        ),
        sa.ForeignKeyConstraint(["relation_id"], ["knowledge_relations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def upgrade() -> None:
    tables = _table_names()

    if "knowledge_content_nodes" not in tables:
        _create_content_nodes()
    else:
        _require_columns(
            "knowledge_content_nodes",
            {
                "id", "entity_type", "entity_id", "display_name", "is_active",
                "source_updated_at", "content_digest", "created_at", "updated_at",
            },
        )

    if "knowledge_relations" not in tables:
        _create_relations()
    else:
        _require_columns(
            "knowledge_relations",
            {
                "id", "source_node_id", "target_node_id", "relation_type", "is_directed",
                "strength", "confidence", "origin", "evidence_summary", "evidence",
                "quality_version", "status", "created_by", "reviewed_by", "approved_at",
                "retired_at", "legacy_reference", "created_at", "updated_at",
            },
        )

    if "knowledge_relation_reviews" not in tables:
        _create_relation_reviews()
    else:
        _require_columns(
            "knowledge_relation_reviews",
            {
                "id", "relation_id", "reviewer_id", "action", "reason", "before_snapshot",
                "after_snapshot", "created_at",
            },
        )

    _create_index_if_missing(
        "knowledge_content_nodes",
        "ix_knowledge_content_nodes_is_active",
        ["is_active"],
    )
    _create_index_if_missing(
        "knowledge_content_nodes",
        "ix_knowledge_content_nodes_type_active",
        ["entity_type", "is_active"],
    )
    _create_index_if_missing(
        "knowledge_relations",
        "ix_knowledge_relations_source_status",
        ["source_node_id", "status"],
    )
    _create_index_if_missing(
        "knowledge_relations",
        "ix_knowledge_relations_target_status",
        ["target_node_id", "status"],
    )
    _create_index_if_missing("knowledge_relations", "ix_knowledge_relations_status", ["status"])
    _create_index_if_missing("knowledge_relations", "ix_knowledge_relations_created_by", ["created_by"])
    _create_index_if_missing("knowledge_relations", "ix_knowledge_relations_reviewed_by", ["reviewed_by"])
    _create_index_if_missing(
        "knowledge_relations",
        "uq_knowledge_relation_active_pair",
        ["source_node_id", "target_node_id", "relation_type", "is_directed"],
        unique=True,
        postgresql_where=sa.text("status IN ('draft', 'pending_review', 'approved')"),
    )
    _create_index_if_missing(
        "knowledge_relation_reviews",
        "ix_knowledge_relation_reviews_relation_created",
        ["relation_id", "created_at"],
    )
    _create_index_if_missing(
        "knowledge_relation_reviews",
        "ix_knowledge_relation_reviews_reviewer_id",
        ["reviewer_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    existing = _table_names()
    protected_tables = [
        table_name
        for table_name in (
            "knowledge_relation_reviews",
            "knowledge_relations",
            "knowledge_content_nodes",
        )
        if table_name in existing
    ]
    for table_name in protected_tables:
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
        if count:
            raise RuntimeError(
                "지식 관계 데이터가 있어 migration downgrade를 중단했습니다. "
                "관계를 retired 처리하거나 백업 복구 절차를 사용하세요."
            )

    for table_name in protected_tables:
        op.drop_table(table_name)
