"""개인 알림 설정·멱등 전달·최초 이벤트 기준선. 기존 email 값은 보존한다."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "20261013_005"
down_revision = "20261013_004"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"]: column for column in sa.inspect(op.get_bind()).get_columns("users")}
    for name in ("comment_notifications", "competition_notifications", "badge_notifications"):
        existing = columns.get(name)
        if existing is not None:
            default = str(existing.get("default", "")).strip().lower()
            if (not isinstance(existing["type"], sa.Boolean) or existing["nullable"]
                    or default not in {"true", "'true'", "'true'::boolean"}):
                raise RuntimeError(f"{name}의 기존 타입·null·기본값 계약이 다릅니다.")
        else:
            op.add_column("users", sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_table("notification_policies",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("baseline_completed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("INSERT INTO notification_policies (id) VALUES ('personal-v1')")
    op.create_table("personal_notifications",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.String(180), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("path", sa.String(300), nullable=False),
        sa.Column("visible", sa.Boolean(), nullable=False),
        sa.Column("email_status", sa.String(20), nullable=False),
        sa.Column("email_attempt_count", sa.Integer(), nullable=False),
        sa.Column("email_next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("email_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "event_id", name="uq_personal_notifications_user_event"),
        sa.CheckConstraint("kind IN ('comment','competition','badge')", name="ck_personal_notifications_kind"),
        sa.CheckConstraint("email_status IN ('pending','sending','sent','skipped','uncertain')", name="ck_personal_notifications_email_status"))
    op.create_index("ix_personal_notifications_user_created", "personal_notifications", ["user_id", "created_at"])
    op.create_index("ix_personal_notifications_email_pending", "personal_notifications", ["email_status", "email_next_attempt_at"])
    op.create_table("competition_notification_states",
        sa.Column("competition_id", pg.UUID(as_uuid=True), sa.ForeignKey("competitions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("published_recorded", sa.Boolean(), nullable=False),
        sa.Column("started_recorded", sa.Boolean(), nullable=False))
    # 기존 공개와 과거 시작은 새 이벤트로 소급 전달하지 않는다.
    op.execute("INSERT INTO competition_notification_states (competition_id,published_recorded,started_recorded) "
               "SELECT id, is_published, start_at <= NOW() FROM competitions")
    op.create_table("personal_notification_events",
        sa.Column("event_id", sa.String(180), primary_key=True),
        sa.Column("competition_id", pg.UUID(as_uuid=True), sa.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("path", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_user_id", pg.UUID(as_uuid=True)),
        sa.Column("complete", sa.Boolean(), nullable=False))
    op.create_table("badge_observations",
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("badge_id", sa.String(80), primary_key=True),
        sa.Column("earned", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("earned_at", sa.DateTime(timezone=True)))


def downgrade():
    bind = op.get_bind()
    for table in ("personal_notifications", "personal_notification_events", "badge_observations"):
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar_one():
            raise RuntimeError("알림 기록이 있어 downgrade로 삭제할 수 없습니다.")
    for table in ("badge_observations", "personal_notification_events", "competition_notification_states",
                  "personal_notifications", "notification_policies"):
        op.drop_table(table)
    for name in ("badge_notifications", "competition_notifications", "comment_notifications"):
        op.drop_column("users", name)
