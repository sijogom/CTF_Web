"""이벤트의 등록 카테고리와 표시명·정렬·변경 이력을 추가한다."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261013_029"
down_revision = "20261013_028"
branch_labels = None
depends_on = None

DEFAULT_CATEGORIES = (
    ("workshop", {"ko": "워크숍", "en": "Workshop", "ja": "ワークショップ", "zh": "工作坊"}),
    ("seminar", {"ko": "세미나", "en": "Seminar", "ja": "セミナー", "zh": "研讨会"}),
    ("study", {"ko": "스터디", "en": "Study", "ja": "勉強会", "zh": "学习小组"}),
    ("networking", {"ko": "네트워킹", "en": "Networking", "ja": "ネットワーキング", "zh": "交流活动"}),
    ("notice", {"ko": "안내", "en": "Notice", "ja": "お知らせ", "zh": "通知"}),
    ("other", {"ko": "기타", "en": "Other", "ja": "その他", "zh": "其他"}),
)


def upgrade():
    op.create_table(
        "platform_event_categories",
        sa.Column("key", sa.String(30), primary_key=True),
        sa.Column("labels", postgresql.JSONB(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("create_request_hash", sa.String(64), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("revision >= 1", name="ck_platform_event_categories_revision"),
        sa.CheckConstraint("sort_order >= 0 AND sort_order <= 1000000", name="ck_platform_event_categories_sort_order"),
        sa.UniqueConstraint("operation_id", name="uq_platform_event_categories_operation"),
    )
    categories = sa.table("platform_event_categories", sa.column("key", sa.String), sa.column("labels", postgresql.JSONB), sa.column("sort_order", sa.Integer))
    op.bulk_insert(categories, [{"key": key, "labels": labels, "sort_order": index} for index, (key, labels) in enumerate(DEFAULT_CATEGORIES)])
    op.create_index("ix_platform_event_categories_order", "platform_event_categories", ["sort_order", "key"])
    op.execute("CREATE UNIQUE INDEX uq_platform_event_categories_ko_label ON platform_event_categories (lower(labels->>'ko'))")
    op.create_table(
        "platform_event_category_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("category_key", sa.String(30), sa.ForeignKey("platform_event_categories.key", ondelete="RESTRICT"), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_platform_event_category_audit_events_category_key", "platform_event_category_audit_events", ["category_key"])
    # PostgreSQL validates every existing category before the fixed enum check is removed.
    op.create_foreign_key("fk_platform_events_category_registry", "platform_events", "platform_event_categories", ["category"], ["key"], ondelete="RESTRICT")
    op.drop_constraint("ck_platform_events_category", "platform_events", type_="check")


def downgrade():
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT key, labels, sort_order, revision FROM platform_event_categories ORDER BY sort_order, key")).mappings().all()
    expected = [{"key": key, "labels": labels, "sort_order": index, "revision": 1} for index, (key, labels) in enumerate(DEFAULT_CATEGORIES)]
    audit_count = bind.execute(sa.text("SELECT count(*) FROM platform_event_category_audit_events")).scalar_one()
    if audit_count or [dict(row) for row in rows] != expected:
        raise RuntimeError("카테고리와 감사 기록 보존을 위해 destructive downgrade is refused")
    op.create_check_constraint("ck_platform_events_category", "platform_events", "category IN ('workshop','seminar','study','networking','notice','other')")
    op.drop_constraint("fk_platform_events_category_registry", "platform_events", type_="foreignkey")
    op.drop_table("platform_event_category_audit_events")
    op.drop_table("platform_event_categories")
