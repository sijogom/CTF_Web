"""지식 태그 매핑 attach/detach append-only 감사 원장."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260814_018"
down_revision = "20260814_017"
branch_labels = None
depends_on = None


TABLE = "knowledge_tag_mapping_audit_events"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(length=16), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mapping_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"],
            ["knowledge_tags.id"],
            ondelete="RESTRICT",
            name="fk_knowledge_tag_mapping_audit_tag",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_tag_mapping_audit_events"),
        sa.CheckConstraint(
            "entity_type IN ('challenge', 'course', 'lesson')",
            name="ck_knowledge_tag_mapping_audit_entity_type",
        ),
        sa.CheckConstraint(
            "action IN ('attach', 'detach')",
            name="ck_knowledge_tag_mapping_audit_action",
        ),
    )
    op.create_index(
        "ix_knowledge_tag_mapping_audit_entity_created",
        TABLE,
        ["entity_type", "entity_id", "created_at"],
    )
    op.create_index(
        "ix_knowledge_tag_mapping_audit_actor_created",
        TABLE,
        ["actor_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar_one()
    if count:
        raise RuntimeError("지식 태그 매핑 감사 원장에 행이 있어 downgrade할 수 없습니다.")
    op.drop_index("ix_knowledge_tag_mapping_audit_actor_created", table_name=TABLE)
    op.drop_index("ix_knowledge_tag_mapping_audit_entity_created", table_name=TABLE)
    op.drop_table(TABLE)
