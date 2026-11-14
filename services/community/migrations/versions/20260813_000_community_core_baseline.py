"""Community legacy core schema baseline.

빈 Community DB에는 현 모델과 동등한 고정 DDL을 생성한다. 기존 DB에는 데이터 변경 없이
legacy 핵심 table/column만 검사한다. 부분 schema 또는 예상과 다른 schema를 자동 복구하지
않는다. 실제 운영 DB에서 이 revision을 실행한 검증은 아직 없다.
"""
from __future__ import annotations

import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260813_000"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


LEGACY_TABLES = frozenset(
    {
        "posts",
        "comments",
        "likes",
        "dislikes",
        "bookmarks",
        "reports",
        "marketplace_items",
        "marketplace_purchases",
        "marketplace_comments",
        "inquiries",
        "inquiry_replies",
        "inquiry_events",
    }
)


EXPECTED_COLUMNS = {
    "posts": {
        "id": ("uuid", False), "title": ("char", False), "content": ("text", False),
        "board_type": ("char", False), "author_id": ("uuid", False), "author_name": ("char", False),
        "challenge_id": ("int", True), "like_count": ("int", False), "comment_count": ("int", False),
        "view_count": ("int", False), "is_anonymous": ("bool", False), "is_hidden": ("bool", False),
        "is_pinned": ("bool", False), "status": ("char", False), "rejection_reason": ("text", True),
        "reviewed_by": ("uuid", True), "reviewed_at": ("timestamp", True), "created_at": ("timestamp", False),
        "updated_at": ("timestamp", True),
    },
    "comments": {
        "id": ("uuid", False), "post_id": ("uuid", False), "parent_id": ("uuid", True),
        "author_id": ("uuid", False), "author_name": ("char", False), "content": ("text", False),
        "is_anonymous": ("bool", False), "is_hidden": ("bool", False), "is_deleted": ("bool", False),
        "created_at": ("timestamp", False), "updated_at": ("timestamp", True),
    },
    "likes": {
        "id": ("uuid", False), "user_id": ("uuid", False), "post_id": ("uuid", False),
        "created_at": ("timestamp", False),
    },
    "dislikes": {
        "id": ("uuid", False), "user_id": ("uuid", False), "post_id": ("uuid", False),
        "created_at": ("timestamp", False),
    },
    "bookmarks": {
        "id": ("uuid", False), "user_id": ("uuid", False), "post_id": ("uuid", False),
        "created_at": ("timestamp", False),
    },
    "reports": {
        "id": ("uuid", False), "reporter_id": ("uuid", False), "target_type": ("char", False),
        "target_id": ("uuid", False), "reason": ("char", False), "detail": ("text", True),
        "status": ("char", False), "reviewed_by": ("uuid", True), "created_at": ("timestamp", False),
        "updated_at": ("timestamp", True),
    },
    "marketplace_items": {
        "id": ("uuid", False), "title": ("char", False), "description": ("text", False),
        "price": ("int", False), "file_url": ("char", True), "original_filename": ("char", True),
        "seller_id": ("uuid", False), "seller_name": ("char", False), "category": ("char", False),
        "status": ("char", False), "download_count": ("int", False), "created_at": ("timestamp", False),
        "updated_at": ("timestamp", True),
    },
    "marketplace_purchases": {
        "id": ("uuid", False), "item_id": ("uuid", False), "buyer_id": ("uuid", False),
        "price_paid": ("int", False), "seller_earned": ("int", False), "purchased_at": ("timestamp", False),
    },
    "marketplace_comments": {
        "id": ("uuid", False), "item_id": ("uuid", False), "author_id": ("uuid", False),
        "author_name": ("char", False), "content": ("text", False), "is_anonymous": ("bool", False),
        "is_hidden": ("bool", False), "is_deleted": ("bool", False), "created_at": ("timestamp", False),
        "updated_at": ("timestamp", True),
    },
    "inquiries": {
        "id": ("uuid", False), "user_id": ("uuid", True), "email": ("char", False),
        "name": ("char", False), "subject": ("char", False), "content": ("text", False),
        "status": ("char", False), "created_at": ("timestamp", False), "updated_at": ("timestamp", True),
    },
    "inquiry_replies": {
        "id": ("uuid", False), "inquiry_id": ("uuid", False), "admin_id": ("uuid", False),
        "admin_name": ("char", False), "content": ("text", False), "created_at": ("timestamp", False),
    },
    "inquiry_events": {
        "id": ("uuid", False), "inquiry_id": ("uuid", False), "actor_type": ("char", False),
        "actor_id": ("uuid", True), "action": ("char", False), "details": ("json", True),
        "created_at": ("timestamp", False),
    },
}

EXPECTED_INDEX_COLUMNS = {
    "posts": {("board_type",), ("author_id",), ("status",)},
    "comments": {("post_id",)},
    "likes": {("user_id",), ("post_id",)},
    "dislikes": {("user_id",), ("post_id",)},
    "bookmarks": {("user_id",), ("post_id",)},
    "reports": {("reporter_id",)},
    "marketplace_items": {("seller_id",)},
    "marketplace_purchases": {("item_id",), ("buyer_id",)},
    "marketplace_comments": {("item_id",), ("author_id",)},
    "inquiries": {("user_id",), ("status",)},
    "inquiry_replies": {("inquiry_id",)},
    "inquiry_events": {("inquiry_id",), ("actor_id",), ("action",)},
}

EXPECTED_FOREIGN_KEYS = {
    "comments": {(("post_id",), "posts"), (("parent_id",), "comments")},
    "likes": {(("post_id",), "posts")},
    "dislikes": {(("post_id",), "posts")},
    "bookmarks": {(("post_id",), "posts")},
    "marketplace_purchases": {(("item_id",), "marketplace_items")},
    "marketplace_comments": {(("item_id",), "marketplace_items")},
    "inquiry_replies": {(("inquiry_id",), "inquiries")},
    "inquiry_events": {(("inquiry_id",), "inquiries")},
}

EXPECTED_UNIQUE_COLUMNS = {
    "likes": {("post_id", "user_id")},
    "dislikes": {("post_id", "user_id")},
    "bookmarks": {("post_id", "user_id")},
    "marketplace_purchases": {("buyer_id", "item_id")},
}


def _table_names(bind) -> set[str]:
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def _require_existing_contract(bind) -> None:
    inspector = sa.inspect(bind)
    present = _table_names(bind) & LEGACY_TABLES
    if present != LEGACY_TABLES:
        missing = ", ".join(sorted(LEGACY_TABLES - present))
        raise RuntimeError(
            "Community legacy schema is partial or incompatible; refusing automatic repair. "
            f"Missing tables: {missing or 'none'}"
        )

    for table_name, expected in EXPECTED_COLUMNS.items():
        actual = {column["name"]: column for column in inspector.get_columns(table_name)}
        missing = set(expected) - set(actual)
        if missing:
            raise RuntimeError(
                f"Community table {table_name} lacks required columns: {', '.join(sorted(missing))}"
            )
        for column_name, (type_fragment, nullable) in expected.items():
            column = actual[column_name]
            if type_fragment not in str(column["type"]).lower() or column.get("nullable") != nullable:
                raise RuntimeError(
                    f"Community column contract mismatch: {table_name}.{column_name} "
                    f"(type={column['type']}, nullable={column.get('nullable')})"
                )

        primary_key = tuple(inspector.get_pk_constraint(table_name).get("constrained_columns") or ())
        if primary_key != ("id",):
            raise RuntimeError(f"Community table {table_name} must have id as its primary key")

        existing_indexes = {
            tuple(index.get("column_names") or ())
            for index in inspector.get_indexes(table_name)
        }
        missing_indexes = EXPECTED_INDEX_COLUMNS.get(table_name, set()) - existing_indexes
        if missing_indexes:
            raise RuntimeError(f"Community table {table_name} lacks required indexes: {sorted(missing_indexes)}")

        existing_foreign_keys = {
            (tuple(foreign_key.get("constrained_columns") or ()), foreign_key.get("referred_table"))
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        missing_foreign_keys = EXPECTED_FOREIGN_KEYS.get(table_name, set()) - existing_foreign_keys
        if missing_foreign_keys:
            raise RuntimeError(f"Community table {table_name} lacks required foreign keys: {sorted(missing_foreign_keys)}")

        existing_unique = {
            tuple(sorted(constraint.get("column_names") or ()))
            for constraint in inspector.get_unique_constraints(table_name)
        }
        missing_unique = EXPECTED_UNIQUE_COLUMNS.get(table_name, set()) - existing_unique
        if missing_unique:
            raise RuntimeError(f"Community table {table_name} lacks required unique constraints: {sorted(missing_unique)}")


def _uuid_column(name: str = "id", *, primary_key: bool = False, nullable: bool = False):
    return sa.Column(name, postgresql.UUID(as_uuid=True), primary_key=primary_key, nullable=nullable)


def _created_at_column() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)


def _create_core_tables() -> None:
    op.create_table(
        "posts",
        _uuid_column(primary_key=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("board_type", sa.String(length=20), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_name", sa.String(length=50), nullable=False),
        sa.Column("language", sa.String(length=5), nullable=False, server_default="ko"),
        sa.Column("title_i18n", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("content_i18n", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("challenge_id", sa.Integer(), nullable=True),
        sa.Column("like_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dislike_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comment_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_anonymous", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="approved"),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        _created_at_column(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_posts_board_type", "posts", ["board_type"])
    op.create_index("ix_posts_author_id", "posts", ["author_id"])
    op.create_index("ix_posts_status", "posts", ["status"])

    op.create_table(
        "comments",
        _uuid_column(primary_key=True),
        sa.Column("post_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("comments.id", ondelete="CASCADE"), nullable=True),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_name", sa.String(length=50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("is_anonymous", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        _created_at_column(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_comments_post_id", "comments", ["post_id"])

    for table_name, constraint_name in (("likes", "uq_user_like"), ("dislikes", "uq_user_dislike"), ("bookmarks", "uq_user_bookmark")):
        op.create_table(
            table_name,
            _uuid_column(primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("post_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False),
            _created_at_column(),
            sa.UniqueConstraint("user_id", "post_id", name=constraint_name),
        )
        op.create_index(f"ix_{table_name}_user_id", table_name, ["user_id"])
        op.create_index(f"ix_{table_name}_post_id", table_name, ["post_id"])

    op.create_table(
        "reports",
        _uuid_column(primary_key=True),
        sa.Column("reporter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
        _created_at_column(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_reports_reporter_id", "reports", ["reporter_id"])

    op.create_table(
        "marketplace_items",
        _uuid_column(primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("file_url", sa.String(length=500), nullable=True),
        sa.Column("original_filename", sa.String(length=255), nullable=True),
        sa.Column("seller_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seller_name", sa.String(length=50), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False, server_default="etc"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("download_count", sa.Integer(), nullable=False, server_default="0"),
        _created_at_column(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_marketplace_items_seller_id", "marketplace_items", ["seller_id"])

    op.create_table(
        "marketplace_purchases",
        _uuid_column(primary_key=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("marketplace_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("buyer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("price_paid", sa.Integer(), nullable=False),
        sa.Column("seller_earned", sa.Integer(), nullable=False),
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("item_id", "buyer_id", name="uq_purchase_item_buyer"),
    )
    op.create_index("ix_marketplace_purchases_item_id", "marketplace_purchases", ["item_id"])
    op.create_index("ix_marketplace_purchases_buyer_id", "marketplace_purchases", ["buyer_id"])

    op.create_table(
        "marketplace_comments",
        _uuid_column(primary_key=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("marketplace_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_name", sa.String(length=50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("is_anonymous", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        _created_at_column(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_marketplace_comments_item_id", "marketplace_comments", ["item_id"])
    op.create_index("ix_marketplace_comments_author_id", "marketplace_comments", ["author_id"])

    op.create_table(
        "inquiries",
        _uuid_column(primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("subject", sa.String(length=300), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        _created_at_column(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_inquiries_user_id", "inquiries", ["user_id"])
    op.create_index("ix_inquiries_status", "inquiries", ["status"])

    op.create_table(
        "inquiry_replies",
        _uuid_column(primary_key=True),
        sa.Column("inquiry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("inquiries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("admin_name", sa.String(length=100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        _created_at_column(),
    )
    op.create_index("ix_inquiry_replies_inquiry_id", "inquiry_replies", ["inquiry_id"])

    _create_inquiry_events()


def _create_inquiry_events() -> None:
    op.create_table(
        "inquiry_events",
        _uuid_column(primary_key=True),
        sa.Column("inquiry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("inquiries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        _created_at_column(),
    )
    op.create_index("ix_inquiry_events_inquiry_id", "inquiry_events", ["inquiry_id"])
    op.create_index("ix_inquiry_events_actor_id", "inquiry_events", ["actor_id"])
    op.create_index("ix_inquiry_events_action", "inquiry_events", ["action"])


def upgrade() -> None:
    bind = op.get_bind()
    present = _table_names(bind) & LEGACY_TABLES
    if not present:
        _create_core_tables()
        return
    if (
        os.getenv("COMMUNITY_LEGACY_ADOPT") == "1"
        and present == LEGACY_TABLES - {"inquiry_events"}
    ):
        # Only the exact known pre-event schema may take this operator-approved
        # additive repair. Any other partial/unknown shape remains fail-closed.
        _create_inquiry_events()
        _require_existing_contract(bind)
        return
    _require_existing_contract(bind)


def downgrade() -> None:
    raise RuntimeError(
        "Community core baseline is not downgradeable. Restore an approved PostgreSQL backup instead."
    )
