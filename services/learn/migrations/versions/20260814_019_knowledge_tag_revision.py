"""지식 태그 자체 변경을 그래프 revision에 포함한다."""

from alembic import op
import sqlalchemy as sa


revision = "20260814_019"
down_revision = "20260814_018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("knowledge_tags")}
    if "updated_at" in columns:
        return
    op.add_column(
        "knowledge_tags",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("knowledge_tags")}
    if "updated_at" in columns:
        op.drop_column("knowledge_tags", "updated_at")
