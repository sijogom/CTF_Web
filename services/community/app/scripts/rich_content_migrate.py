"""Inventory, apply, or roll back Community canonical HTML conversion."""

from app.config import settings
from app.database import engine
from app.models.inquiry import Inquiry, InquiryReply
from app.models.marketplace import MarketplaceComment, MarketplaceItem
from app.models.post import Comment, Post
from app.utils.rich_content_migration import RichContentFieldSpec, run_cli


SPECS = (
    RichContentFieldSpec(
        Post.__table__,
        "content",
        field_name="게시글 본문",
        guard_columns=("author_id", "status", "reviewed_by", "created_at", "updated_at"),
    ),
    RichContentFieldSpec(
        Post.__table__,
        "content_i18n",
        kind="mapping",
        field_name="게시글 본문 번역",
        allow_empty=True,
        guard_columns=("author_id", "status", "reviewed_by", "created_at", "updated_at"),
    ),
    RichContentFieldSpec(
        Comment.__table__,
        "content",
        field_name="댓글 내용",
        guard_columns=("post_id", "author_id", "is_hidden", "is_deleted", "created_at", "updated_at"),
    ),
    RichContentFieldSpec(
        MarketplaceItem.__table__,
        "description",
        field_name="상품 설명",
        allow_empty=True,
        max_visible_chars=5000,
        guard_columns=("seller_id", "status", "created_at", "updated_at"),
    ),
    RichContentFieldSpec(
        MarketplaceComment.__table__,
        "content",
        field_name="장터 댓글",
        max_visible_chars=2000,
        guard_columns=("item_id", "author_id", "is_hidden", "is_deleted", "created_at", "updated_at"),
    ),
    RichContentFieldSpec(
        Inquiry.__table__,
        "content",
        field_name="문의 내용",
        max_visible_chars=5000,
        guard_columns=("user_id", "status", "created_at", "updated_at"),
    ),
    RichContentFieldSpec(
        InquiryReply.__table__,
        "content",
        field_name="답변 내용",
        max_visible_chars=5000,
        guard_columns=("inquiry_id", "admin_id", "created_at"),
    ),
)


if __name__ == "__main__":
    run_cli(
        service="community",
        environment=settings.environment,
        engine=engine,
        specs=SPECS,
    )
