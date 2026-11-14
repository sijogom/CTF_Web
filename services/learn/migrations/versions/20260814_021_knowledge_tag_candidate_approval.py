"""자동 태그 후보를 승인해 실제 junction에 반영하기 위한 provenance 확장."""

from alembic import op
import sqlalchemy as sa


revision = "20260814_021"
down_revision = "20260814_020"
branch_labels = None
depends_on = None


MAPPING_TABLES = {
    "challenge_knowledge_tags": "ck_challenge_knowledge_tag_origin",
    "course_knowledge_tags": "ck_course_knowledge_tag_origin",
    "lesson_knowledge_tags": "ck_lesson_knowledge_tag_origin",
}
ORIGIN_CHECK = "origin IN ('manual', 'import', 'rule', 'model_candidate')"
LEGACY_ORIGIN_CHECK = "origin IN ('manual', 'import')"


def _check_names(bind, table: str) -> set[str]:
    return {
        item.get("name")
        for item in sa.inspect(bind).get_check_constraints(table)
        if item.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    present = tables & set(MAPPING_TABLES)
    if present and present != set(MAPPING_TABLES):
        raise RuntimeError("knowledge tag mapping tables are partially present; refusing automatic repair")
    if not present:
        return
    for table, constraint_name in MAPPING_TABLES.items():
        if constraint_name in _check_names(bind, table):
            op.drop_constraint(constraint_name, table, type_="check")
        op.create_check_constraint(constraint_name, table, ORIGIN_CHECK)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if not set(MAPPING_TABLES).issubset(tables):
        return
    for table in MAPPING_TABLES:
        rule_count = bind.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE origin IN ('rule', 'model_candidate')"
            )
        ).scalar_one()
        if rule_count:
            raise RuntimeError("approved automatic tag mappings exist; destructive downgrade is refused")
    for table, constraint_name in MAPPING_TABLES.items():
        if constraint_name in _check_names(bind, table):
            op.drop_constraint(constraint_name, table, type_="check")
        op.create_check_constraint(constraint_name, table, LEGACY_ORIGIN_CHECK)
