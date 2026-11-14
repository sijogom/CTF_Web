"""Absorb the legacy posts columns previously added at application startup."""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260813_001"
down_revision: Union[str, None] = "20260813_000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EXPECTED = {
    "dislike_count": {"nullable": False, "default": "0"},
    "language": {"nullable": False, "default": "ko"},
    "title_i18n": {"nullable": False, "default": "{}"},
    "content_i18n": {"nullable": False, "default": "{}"},
}


def _posts_columns(bind) -> dict[str, dict]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns("posts")}


def _assert_existing_column_contract(name: str, column: dict) -> None:
    if column.get("nullable"):
        raise RuntimeError(f"posts.{name} is nullable; refusing automatic schema conversion")
    type_name = str(column["type"]).lower()
    if name in {"dislike_count"} and "int" not in type_name:
        raise RuntimeError(f"posts.{name} has unexpected type {column['type']}")
    if name == "language" and "char" not in type_name:
        raise RuntimeError(f"posts.{name} has unexpected type {column['type']}")
    if name in {"title_i18n", "content_i18n"} and "json" not in type_name:
        raise RuntimeError(f"posts.{name} has unexpected type {column['type']}")


def upgrade() -> None:
    bind = op.get_bind()
    columns = _posts_columns(bind)
    additions = {
        "dislike_count": sa.Column("dislike_count", sa.Integer(), nullable=False, server_default="0"),
        "language": sa.Column("language", sa.String(length=5), nullable=False, server_default="ko"),
        "title_i18n": sa.Column(
            "title_i18n",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        "content_i18n": sa.Column(
            "content_i18n",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    }
    for name, definition in additions.items():
        existing = columns.get(name)
        if existing is None:
            op.add_column("posts", definition)
        else:
            _assert_existing_column_contract(name, existing)


def downgrade() -> None:
    raise RuntimeError(
        "Legacy posts columns are intentionally retained. Restore an approved PostgreSQL backup instead."
    )
