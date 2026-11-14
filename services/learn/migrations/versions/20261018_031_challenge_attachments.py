"""일반 첨부 원장. 기존 files와 객체는 변환하거나 삭제하지 않는다."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261018_031"
down_revision = "20261018_030"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("challenges", sa.Column("attachment_revision", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "challenge_attachment_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("challenge_id", sa.Integer(), nullable=True),
        sa.Column("original_challenge_id", sa.Integer(), nullable=True),
        sa.Column("base_attachment_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(16), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('open','bound')", name="ck_challenge_attachment_draft_state"),
        sa.CheckConstraint("base_attachment_revision >= 0", name="ck_challenge_attachment_draft_revision"),
    )
    op.create_index("ix_challenge_attachment_drafts_owner_id", "challenge_attachment_drafts", ["owner_id"])
    op.create_table(
        "challenge_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("challenge_attachment_drafts.id"), nullable=False),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("object_path", sa.Text(), nullable=False, unique=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("challenge_id", sa.Integer(), sa.ForeignKey("challenges.id", ondelete="SET NULL"), nullable=True),
        sa.Column("bound_challenge_id", sa.Integer(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("byte_size >= 0 AND byte_size <= 104857600", name="ck_challenge_attachment_size"),
        sa.CheckConstraint("state IN ('pending','ready')", name="ck_challenge_attachment_state"),
    )
    op.create_index("ix_challenge_attachments_draft_id", "challenge_attachments", ["draft_id"])
    op.create_index("ix_challenge_attachments_parent_position", "challenge_attachments", ["challenge_id", "position"])


def downgrade():
    raise RuntimeError("첨부 원장과 원본 파일을 보존하기 위해 자동 downgrade를 지원하지 않습니다.")
