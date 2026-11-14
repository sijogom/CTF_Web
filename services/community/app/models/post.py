import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, Boolean, DateTime, Text, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class PostStatus(str, enum.Enum):
    """게시글 승인 상태."""
    DRAFT = "draft"          # 작성 중 (작성자만 볼 수 있음)
    PENDING = "pending"      # 승인 대기 (관리자 검토 필요 — writeup 기본값)
    APPROVED = "approved"    # 승인됨 (일반 사용자 열람 가능 — free/qna/notice 기본값)
    REJECTED = "rejected"    # 거부됨 (사유와 함께 작성자에게 통보)


# 자동 승인 대상 게시판 유형 (자유 게시)
AUTO_APPROVE_BOARD_TYPES = frozenset({"free", "qna", "notice"})

# 관리자 승인 필요 게시판 유형
REQUIRE_APPROVAL_BOARD_TYPES = frozenset({"writeup"})


class Post(Base):
    __tablename__ = "posts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(300), nullable=False)
    content = Column(Text, nullable=False)
    board_type = Column(String(20), nullable=False, index=True)  # free, qna, writeup, notice
    author_id = Column(UUID(as_uuid=True), nullable=False, index=True)  # 크로스 DB
    author_name = Column(String(50), nullable=False)  # 비정규화 (표시용)
    language = Column(String(5), default="ko", nullable=False)  # 작성 언어 (ko, en, ja, zh)
    title_i18n = Column(JSONB, nullable=False, default=dict)  # {"ko": "...", "en": "...", ...}
    content_i18n = Column(JSONB, nullable=False, default=dict)  # {"ko": "...", "en": "...", ...}
    # Write-up 관련
    challenge_id = Column(Integer, nullable=True)  # CTFd challenge ID (writeup 전용)
    # 카운터 (비정규화)
    like_count = Column(Integer, default=0, nullable=False)
    dislike_count = Column(Integer, default=0, nullable=False)
    comment_count = Column(Integer, default=0, nullable=False)
    view_count = Column(Integer, default=0, nullable=False)
    # 익명
    is_anonymous = Column(Boolean, default=False, nullable=False)
    # 상태
    is_hidden = Column(Boolean, default=False, nullable=False)
    is_pinned = Column(Boolean, default=False, nullable=False)

    # 콘텐츠 승인 상태
    status = Column(
        String(20),
        default=PostStatus.APPROVED.value,
        nullable=False,
        index=True,
    )
    rejection_reason = Column(Text, nullable=True)           # 거부 사유
    reviewed_by = Column(UUID(as_uuid=True), nullable=True)  # 승인/거부한 관리자 ID
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    comments = relationship("Comment", back_populates="post", order_by="Comment.created_at", cascade="all, delete-orphan", passive_deletes=True)


class Comment(Base):
    __tablename__ = "comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id = Column(UUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("comments.id", ondelete="CASCADE"), nullable=True)  # 대댓글
    author_id = Column(UUID(as_uuid=True), nullable=False)
    author_name = Column(String(50), nullable=False)
    content = Column(Text, nullable=False)
    is_anonymous = Column(Boolean, default=False, nullable=False)
    is_hidden = Column(Boolean, default=False, nullable=False)
    is_deleted = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    post = relationship("Post", back_populates="comments")
    parent = relationship("Comment", remote_side=[id], backref="replies")


class Like(Base):
    __tablename__ = "likes"
    __table_args__ = (
        UniqueConstraint("user_id", "post_id", name="uq_user_like"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    post_id = Column(UUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class Dislike(Base):
    __tablename__ = "dislikes"
    __table_args__ = (
        UniqueConstraint("user_id", "post_id", name="uq_user_dislike"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    post_id = Column(UUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class Bookmark(Base):
    __tablename__ = "bookmarks"
    __table_args__ = (
        UniqueConstraint("user_id", "post_id", name="uq_user_bookmark"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    post_id = Column(UUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class Report(Base):
    __tablename__ = "reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reporter_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    target_type = Column(String(20), nullable=False)  # post, comment
    target_id = Column(UUID(as_uuid=True), nullable=False)
    reason = Column(String(20), nullable=False)  # spam, abuse, inappropriate, other
    detail = Column(Text, nullable=True)
    status = Column(String(20), default="pending", nullable=False)  # pending, reviewed, dismissed
    reviewed_by = Column(UUID(as_uuid=True), nullable=True)  # 처리한 관리자 ID
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
