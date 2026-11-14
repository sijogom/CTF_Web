"""신규 댓글의 개인 알림 transactional outbox. 기존 댓글은 이관하지 않는다."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "20261013_005"
down_revision = "20261013_004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("comment_notification_events",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("recipient_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("post_id", pg.UUID(as_uuid=True), sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("comment_id", pg.UUID(as_uuid=True), sa.ForeignKey("comments.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_comment_notification_events_pending", "comment_notification_events", ["completed_at", "next_attempt_at"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM comment_notification_events)")).scalar_one():
        raise RuntimeError("댓글 알림 기록이 있어 downgrade로 삭제할 수 없습니다.")
    op.drop_table("comment_notification_events")
