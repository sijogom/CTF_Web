"""개인화 추천 동의 현재 상태와 append-only 변경 이력."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260814_020"
down_revision = "20260814_019"
branch_labels = None
depends_on = None


CONSENT_TABLE = "recommendation_consents"
EVENT_TABLE = "recommendation_consent_events"
EXPECTED_COLUMNS = {
    CONSENT_TABLE: {
        "id", "user_id", "scope", "policy_version", "enabled", "granted_at",
        "withdrawn_at", "created_at", "updated_at",
    },
    EVENT_TABLE: {"id", "consent_id", "user_id", "action", "policy_version", "created_at"},
}


def _assert_complete_or_absent(bind) -> bool:
    tables = set(sa.inspect(bind).get_table_names())
    present = tables & set(EXPECTED_COLUMNS)
    if not present:
        return False
    if present != set(EXPECTED_COLUMNS):
        raise RuntimeError("recommendation consent tables are partially present; refusing automatic repair")
    inspector = sa.inspect(bind)
    for table, columns in EXPECTED_COLUMNS.items():
        actual = {column["name"] for column in inspector.get_columns(table)}
        if actual != columns:
            raise RuntimeError(f"{table} schema is incomplete; refusing automatic repair")
    return True


def upgrade() -> None:
    bind = op.get_bind()
    if _assert_complete_or_absent(bind):
        return

    op.create_table(
        CONSENT_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", sa.String(length=64), server_default="learning_recommendations", nullable=False),
        sa.Column("policy_version", sa.String(length=64), server_default="learning-recommendations-v1", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_recommendation_consents"),
        sa.UniqueConstraint("user_id", "scope", name="uq_recommendation_consent_user_scope"),
        sa.CheckConstraint("scope = 'learning_recommendations'", name="ck_recommendation_consent_scope"),
    )
    op.create_index("ix_recommendation_consents_user_id", CONSENT_TABLE, ["user_id"])
    op.create_index("ix_recommendation_consents_user_enabled", CONSENT_TABLE, ["user_id", "enabled"])

    op.create_table(
        EVENT_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("consent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["consent_id"],
            [f"{CONSENT_TABLE}.id"],
            ondelete="CASCADE",
            name="fk_recommendation_consent_event_consent",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_recommendation_consent_events"),
        sa.CheckConstraint(
            "action IN ('enabled', 'disabled')",
            name="ck_recommendation_consent_event_action",
        ),
    )
    op.create_index("ix_recommendation_consent_events_user_id", EVENT_TABLE, ["user_id"])
    op.create_index(
        "ix_recommendation_consent_events_user_created",
        EVENT_TABLE,
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_recommendation_consent_events_consent_created",
        EVENT_TABLE,
        ["consent_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (EVENT_TABLE, CONSENT_TABLE):
        if table in set(sa.inspect(bind).get_table_names()):
            count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            if count:
                raise RuntimeError("recommendation consent data exists; destructive downgrade is refused")
    op.drop_table(EVENT_TABLE)
    op.drop_table(CONSENT_TABLE)
