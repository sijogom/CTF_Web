"""태그 삭제와 독립 감사 보존을 지원한다."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261020_032"
down_revision = "20261018_031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 원래 tag_id와 JSON snapshot은 갱신하지 않고 FK만 분리한다.
    audit_table = "knowledge_tag_mapping_audit_events"
    foreign_keys = [item for item in sa.inspect(op.get_bind()).get_foreign_keys(audit_table)
        if item.get("constrained_columns") == ["tag_id"]
        and item.get("referred_table") == "knowledge_tags" and item.get("referred_columns") == ["id"]]
    if len(foreign_keys) != 1 or not foreign_keys[0].get("name"):
        raise RuntimeError("기존 태그 매핑 감사 FK를 유일하게 확인할 수 없습니다.")
    op.drop_constraint(foreign_keys[0]["name"], audit_table, type_="foreignkey")
    op.create_table(
        "knowledge_tag_deletion_archives",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tag_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("impact_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("graph_revision", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_knowledge_tag_deletion_archives_tag_id", "knowledge_tag_deletion_archives", ["tag_id"])
    op.create_index("ix_knowledge_tag_deletion_archives_actor_id", "knowledge_tag_deletion_archives", ["actor_id"])
    op.drop_constraint("ck_knowledge_projection_outbox_aggregate", "knowledge_projection_outbox", type_="check")
    op.create_check_constraint("ck_knowledge_projection_outbox_aggregate", "knowledge_projection_outbox",
        "aggregate_type IN ('knowledge_relation', 'knowledge_relation_proposal', 'knowledge_tag')")
    op.drop_constraint("ck_knowledge_projection_outbox_event", "knowledge_projection_outbox", type_="check")
    op.create_check_constraint("ck_knowledge_projection_outbox_event", "knowledge_projection_outbox",
        "event_type IN ('proposal_approved', 'proposal_rejected', 'proposal_retired', 'relation_published', 'relation_retired', 'tag_deleted')")


def downgrade() -> None:
    raise RuntimeError("삭제된 태그의 감사 기록을 보존하기 위해 destructive downgrade는 허용하지 않습니다.")
