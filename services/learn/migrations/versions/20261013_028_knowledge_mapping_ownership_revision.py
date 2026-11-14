"""자동 매핑 소유권과 공개 그래프 트랜잭션 revision을 추가한다."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261013_028"
down_revision = "20260816_027"
branch_labels = None
depends_on = None

SOURCES = (
    "challenges", "courses", "lessons", "hints", "knowledge_tags",
    "challenge_knowledge_tags", "course_knowledge_tags", "lesson_knowledge_tags",
    "challenge_prerequisites", "challenge_learning_prerequisites", "course_prerequisites",
    "knowledge_content_nodes", "knowledge_relations", "platform_events", "teaching_classes",
    "education_assignments", "teaching_class_enrollments", "education_role_grants",
)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in ("challenge_knowledge_tags", "course_knowledge_tags", "lesson_knowledge_tags"):
        if "source_relation_id" not in {column["name"] for column in inspector.get_columns(table)}:
            op.add_column(table, sa.Column("source_relation_id", postgresql.UUID(as_uuid=True), nullable=True))
            op.create_foreign_key(f"fk_{table}_source_relation", table, "knowledge_relations", ["source_relation_id"], ["id"], ondelete="SET NULL")
        index = f"ix_{table}_source_relation_id"
        if index not in {item["name"] for item in inspector.get_indexes(table)}:
            op.create_index(index, table, ["source_relation_id"])
        entity_type = {"challenge_knowledge_tags": "challenge", "course_knowledge_tags": "course", "lesson_knowledge_tags": "lesson"}[table]
        # Only an unambiguous, provenance-identical live relation may own a legacy row.
        op.execute(sa.text(f"""
            UPDATE {table} AS m SET source_relation_id = owned.id
            FROM (
                SELECT r.id, s.entity_id, t.entity_id AS tag_id,
                       count(*) OVER (PARTITION BY s.entity_id, t.entity_id) AS owners
                FROM knowledge_relations r
                JOIN knowledge_content_nodes s ON s.id=r.source_node_id
                JOIN knowledge_content_nodes t ON t.id=r.target_node_id
                WHERE r.status='approved' AND r.relation_type='tag_affinity' AND r.is_directed
                  AND s.entity_type=:entity_type AND t.entity_type='knowledge_tag'
            ) owned, knowledge_relations r
            WHERE owned.owners=1 AND r.id=owned.id AND m.source_relation_id IS NULL
              AND m.{entity_type}_id::text=owned.entity_id AND m.tag_id::text=owned.tag_id
              AND m.origin IN ('rule','model_candidate') AND m.origin=r.origin
              AND m.strength=r.strength AND m.confidence=r.confidence
              AND m.evidence_summary IS NOT DISTINCT FROM r.evidence_summary
              AND m.created_by IS NOT DISTINCT FROM r.created_by
        """).bindparams(entity_type=entity_type))
    if "knowledge_graph_revision" not in set(inspector.get_table_names()):
        op.create_table("knowledge_graph_revision", sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
                        sa.Column("revision", sa.BigInteger(), nullable=False),
                        sa.UniqueConstraint("revision", name="uq_knowledge_graph_revision_transaction"))
    elif "uq_knowledge_graph_revision_transaction" not in {item["name"] for item in sa.inspect(bind).get_unique_constraints("knowledge_graph_revision")}:
        op.create_unique_constraint("uq_knowledge_graph_revision_transaction", "knowledge_graph_revision", ["revision"])
    op.execute("""CREATE OR REPLACE FUNCTION bump_knowledge_graph_revision() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          INSERT INTO knowledge_graph_revision(revision) VALUES(txid_current())
          ON CONFLICT(revision) DO NOTHING;
          RETURN NULL;
        END $$""")
    for table in SOURCES:
        op.execute(f"DROP TRIGGER IF EXISTS knowledge_graph_revision_write ON {table}")
        op.execute(f"CREATE TRIGGER knowledge_graph_revision_write AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION bump_knowledge_graph_revision()")


def downgrade():
    raise RuntimeError("자동 매핑 소유권과 revision 보존을 위해 destructive downgrade is refused")
