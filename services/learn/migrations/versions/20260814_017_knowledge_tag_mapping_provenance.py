"""태그 매핑을 그래프가 재사용할 수 있는 provenance 원장으로 확장한다."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260814_017"
down_revision: Union[str, None] = "20260814_016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLES = (
    "challenge_knowledge_tags",
    "course_knowledge_tags",
    "lesson_knowledge_tags",
)
_METADATA_COLUMNS = {
    "direction",
    "strength",
    "confidence",
    "evidence_summary",
    "origin",
    "created_by",
    "approved_at",
}
def _table_names(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _column_names(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _constraint_names(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {
        item["name"]
        for item in inspector.get_check_constraints(table)
        if item.get("name")
    }


def _index_names(bind, table: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(bind).get_indexes(table)
        if item.get("name")
    }


def _add_metadata_columns(table: str) -> None:
    op.add_column(
        table,
        sa.Column(
            "direction",
            sa.String(length=32),
            nullable=False,
            server_default="content_to_knowledge_tag",
        ),
    )
    op.add_column(
        table,
        sa.Column("strength", sa.Numeric(4, 3), nullable=False, server_default="1"),
    )
    op.add_column(
        table,
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False, server_default="1"),
    )
    op.add_column(
        table,
        sa.Column(
            "evidence_summary",
            sa.Text(),
            nullable=False,
            server_default="manual tag mapping",
        ),
    )
    op.add_column(
        table,
        sa.Column("origin", sa.String(length=24), nullable=False, server_default="manual"),
    )
    op.add_column(
        table,
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        table,
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )


def _ensure_constraints_and_index(bind, table: str) -> None:
    names = _constraint_names(bind, table)
    prefix = table.removesuffix("_knowledge_tags")
    constraints = {
        f"ck_{prefix}_knowledge_tag_direction": "direction = 'content_to_knowledge_tag'",
        f"ck_{prefix}_knowledge_tag_strength": "strength >= 0 AND strength <= 1",
        f"ck_{prefix}_knowledge_tag_confidence": "confidence >= 0 AND confidence <= 1",
        f"ck_{prefix}_knowledge_tag_origin": "origin IN ('manual', 'import')",
    }
    for name, expression in constraints.items():
        if name not in names:
            op.create_check_constraint(name, table, expression)

    created_by_index = f"ix_{table}_created_by"
    if created_by_index not in _index_names(bind, table):
        op.create_index(created_by_index, table, ["created_by"])


def upgrade() -> None:
    bind = op.get_bind()
    tables = _table_names(bind)
    missing_tables = sorted(set(_TABLES) - tables)
    if missing_tables:
        raise RuntimeError(
            "지식 태그 매핑 table이 일부 누락되어 provenance migration을 중단합니다: "
            + ",".join(missing_tables)
        )

    for table in _TABLES:
        present = _column_names(bind, table) & _METADATA_COLUMNS
        if present and present != _METADATA_COLUMNS:
            missing = sorted(_METADATA_COLUMNS - present)
            raise RuntimeError(
                f"{table} provenance column이 부분적으로만 존재합니다. 자동 보정하지 않습니다: "
                + ",".join(missing)
            )
        if not present:
            _add_metadata_columns(table)
        _ensure_constraints_and_index(bind, table)


def downgrade() -> None:
    bind = op.get_bind()
    tables = _table_names(bind)
    for table in _TABLES:
        if table not in tables:
            continue
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if count:
            raise RuntimeError(
                f"{table} 태그 매핑 데이터가 있어 provenance migration downgrade를 중단했습니다."
            )
        prefix = table.removesuffix("_knowledge_tags")
        for name in (
            f"ck_{prefix}_knowledge_tag_direction",
            f"ck_{prefix}_knowledge_tag_strength",
            f"ck_{prefix}_knowledge_tag_confidence",
            f"ck_{prefix}_knowledge_tag_origin",
        ):
            if name in _constraint_names(bind, table):
                op.drop_constraint(name, table, type_="check")
        index_name = f"ix_{table}_created_by"
        if index_name in _index_names(bind, table):
            op.drop_index(index_name, table_name=table)
        for column in reversed(sorted(_METADATA_COLUMNS)):
            if column in _column_names(bind, table):
                op.drop_column(table, column)
