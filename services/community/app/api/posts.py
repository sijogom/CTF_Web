"""게시글 CRUD + 콘텐츠 승인 + 자동 번역 API."""

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, select, func, update, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, async_session

logger = logging.getLogger(__name__)
from app.models.post import (
    AUTO_APPROVE_BOARD_TYPES,
    REQUIRE_APPROVAL_BOARD_TYPES,
    Post,
    PostStatus,
    Comment,
    Like,
    Dislike,
    Bookmark,
)
from app.utils.translator import auto_translate, auto_translate_rich_content
from app.utils.safe_html import (
    RichContentError,
    normalize_rich_content_for_write,
    normalize_rich_content_mapping,
    sanitize_rich_content,
)
from app.api.deps import (
    get_current_user_id,
    get_current_username,
    get_optional_user_id,
    get_optional_user_role,
    require_role,
)
from app.api.moderation import assert_direct_owner_delete_allowed
from app.utils.auth_client import verify_user_exists
from app.utils.content_visibility import (
    MODERATION_READ_ACCESS,
    MODERATION_ROLES,
    PUBLIC_ACCESS,
    assert_content_visible,
)

router = APIRouter(tags=["게시글"])


def _escape_like(value: str) -> str:
    """LIKE 패턴에서 특수 와일드카드 문자를 이스케이프한다."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ──── 언어 설정 ────
LANG_LABELS = {"ko": "한국어", "en": "English", "ja": "日本語", "zh": "中文"}


def _localized(i18n_dict: dict | None, fallback: str, lang: str) -> str:
    """i18n dict에서 lang에 해당하는 값을 반환, 없으면 fallback."""
    if i18n_dict and lang in i18n_dict:
        return i18n_dict[lang]
    return fallback


async def _run_auto_translate(post_id: str, title: str, content: str, source_lang: str) -> None:
    """백그라운드에서 게시글 제목/내용을 자동 번역하고 DB에 저장."""
    try:
        title_i18n = await auto_translate(title, source_lang=source_lang)
        content_i18n = await auto_translate_rich_content(content, source_lang=source_lang)

        async with async_session() as session:
            result = await session.execute(
                select(Post).where(Post.id == post_id)
            )
            post = result.scalar_one_or_none()
            if (
                post
                and post.title == title
                and post.content == content
                and (post.language or "ko") == source_lang
            ):
                post.title_i18n = title_i18n
                post.content_i18n = content_i18n
                await session.commit()
                logger.info("게시글 자동 번역 완료: %s", post_id)
    except Exception as error:
        logger.error("게시글 자동 번역 실패: %s — %s", post_id, type(error).__name__)


# ──── 스키마 ────

class PostCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    content: str = Field(..., min_length=1)
    board_type: str = Field(..., pattern=r"^(free|qna|writeup|notice)$")
    challenge_id: int | None = None
    is_anonymous: bool = False
    language: str = Field("ko", pattern=r"^(ko|en|ja|zh)$")

    @field_validator("content")
    @classmethod
    def sanitize_content(cls, value: str) -> str:
        return sanitize_rich_content(value, field_name="게시글 본문")


class PostReject(BaseModel):
    """게시글 거부 요청 (관리자 전용)."""
    rejection_reason: str = Field(..., min_length=1, max_length=1000, description="거부 사유")


class CommentCreate(BaseModel):
    content: str = Field(..., min_length=1)
    parent_id: UUID | None = None
    is_anonymous: bool = False

    @field_validator("content")
    @classmethod
    def sanitize_content(cls, value: str) -> str:
        return sanitize_rich_content(value, field_name="댓글 내용")


class PostResponse(BaseModel):
    id: UUID
    title: str
    content: str
    board_type: str
    author_id: UUID | None
    author_name: str
    challenge_id: int | None
    language: str = "ko"
    like_count: int
    dislike_count: int
    comment_count: int
    view_count: int
    is_pinned: bool
    is_anonymous: bool = False
    status: str = "approved"
    rejection_reason: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    liked: bool = False
    disliked: bool = False
    bookmarked: bool = False

    model_config = {"from_attributes": True}


# 개념글 기준: 추천 - 비추천 >= 이 값
BEST_POST_THRESHOLD = 10


class PostSummary(BaseModel):
    id: UUID
    title: str
    board_type: str
    author_name: str
    language: str = "ko"
    like_count: int
    dislike_count: int
    comment_count: int
    view_count: int
    is_pinned: bool
    is_anonymous: bool = False
    is_best: bool = False
    status: str = "approved"
    created_at: datetime

    model_config = {"from_attributes": True}


class PostListResponse(BaseModel):
    """페이지네이션 포함 게시글 목록 응답."""
    items: list[PostSummary]
    total: int
    page: int
    size: int


class PendingPostListResponse(BaseModel):
    """승인 대기 게시글 목록 응답."""
    items: list[PostSummary]
    total: int
    page: int
    size: int


class ApprovalActionResponse(BaseModel):
    """승인/거부 처리 결과 응답."""
    id: UUID
    status: str
    message: str
    reviewed_by: UUID | None = None
    reviewed_at: datetime | None = None


# ──── 가시성 필터 헬퍼 ────

def _build_visibility_filters(
    user_id: UUID | None,
    user_role: str | None,
) -> list:
    """사용자 역할에 따른 게시글 가시성 필터 조건 목록을 반환한다.

    - admin/moderator: 모든 상태 열람 가능
    - 일반 사용자(로그인): approved + 본인 작성 전체
    - 비로그인: approved만
    """
    if user_role in MODERATION_ROLES:
        return []

    if user_id:
        return [
            or_(
                Post.status == PostStatus.APPROVED.value,
                Post.author_id == user_id,
            )
        ]

    return [Post.status == PostStatus.APPROVED.value]


async def _get_post_for_visibility(
    db: AsyncSession,
    post_id: UUID,
    *,
    user_role: str | None = None,
    moderation_read: bool = False,
    for_update: bool = False,
) -> Post:
    """게시글을 조회하고 숨김 콘텐츠의 공개 접근을 서버에서 차단한다."""
    statement = select(Post).where(Post.id == post_id)
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="게시글을 찾을 수 없습니다.",
        )

    assert_content_visible(
        is_hidden=post.is_hidden,
        role=user_role,
        scope=MODERATION_READ_ACCESS if moderation_read else PUBLIC_ACCESS,
        not_found_detail="게시글을 찾을 수 없습니다.",
    )
    return post


# ──── 엔드포인트 ────

@router.get("/posts", response_model=PostListResponse)
async def list_posts(
    board_type: str | None = Query(None),
    search: str | None = Query(None),
    lang: str = Query("ko", description="응답 언어 (ko, en, ja, zh)"),
    status_filter: str | None = Query(None, alias="status", description="상태 필터 (admin/moderator 전용)"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: UUID | None = Depends(get_optional_user_id),
    user_role: str | None = Depends(get_optional_user_role),
):
    """게시글 목록 조회.

    가시성 규칙:
    - 일반 사용자: approved 상태만 (+ 본인 작성 전체)
    - admin/moderator: 모든 상태 (status 쿼리 파라미터로 필터 가능)
    """
    base_filters = [Post.is_hidden.is_(False)]

    # 역할별 가시성 필터 적용
    for condition in _build_visibility_filters(user_id, user_role):
        base_filters.append(condition)

    # admin/moderator의 상태 필터
    if status_filter and user_role in MODERATION_ROLES:
        base_filters.append(Post.status == status_filter)

    if board_type:
        base_filters.append(Post.board_type == board_type)
    if search:
        escaped = _escape_like(search)
        base_filters.append(Post.title.ilike(f"%{escaped}%", escape="\\"))

    # 총 개수 쿼리
    count_query = select(func.count()).select_from(Post)
    for f in base_filters:
        count_query = count_query.where(f)
    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    # 데이터 쿼리 — admin/moderator는 pending 우선 정렬
    query = select(Post)
    for f in base_filters:
        query = query.where(f)

    if user_role in MODERATION_ROLES:
        status_order = case(
            (Post.status == PostStatus.PENDING.value, 0),
            (Post.status == PostStatus.DRAFT.value, 1),
            (Post.status == PostStatus.APPROVED.value, 2),
            (Post.status == PostStatus.REJECTED.value, 3),
            else_=4,
        )
        query = query.order_by(
            status_order,
            Post.is_pinned.desc(),
            Post.created_at.desc(),
            Post.id.desc(),
        )
    else:
        query = query.order_by(Post.is_pinned.desc(), Post.created_at.desc(), Post.id.desc())

    query = query.offset((page - 1) * size).limit(size)

    result = await db.execute(query)
    posts = result.scalars().all()

    is_admin_or_mod = user_role in MODERATION_ROLES
    items = [
        PostSummary(
            id=p.id,
            title=_localized(p.title_i18n, p.title, lang),
            board_type=p.board_type,
            author_name=p.author_name if (not p.is_anonymous or is_admin_or_mod) else "익명",
            language=p.language or "ko",
            like_count=p.like_count,
            dislike_count=p.dislike_count,
            comment_count=p.comment_count,
            view_count=p.view_count,
            is_pinned=p.is_pinned,
            is_anonymous=p.is_anonymous,
            is_best=(p.like_count - p.dislike_count) >= BEST_POST_THRESHOLD,
            status=p.status,
            created_at=p.created_at,
        )
        for p in posts
    ]

    return PostListResponse(items=items, total=total, page=page, size=size)


@router.get("/posts/pending", response_model=PendingPostListResponse)
async def list_pending_posts(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """승인 대기(pending) 게시글 목록 조회 (admin/moderator 전용)."""
    base_filters = [
        Post.status == PostStatus.PENDING.value,
        Post.is_hidden.is_(False),
    ]

    count_result = await db.execute(
        select(func.count()).select_from(Post).where(*base_filters)
    )
    total = count_result.scalar() or 0

    query = (
        select(Post)
        .where(*base_filters)
        .order_by(Post.created_at.asc(), Post.id.asc())  # 오래된 것부터 (먼저 제출된 것을 먼저 검토)
        .offset((page - 1) * size)
        .limit(size)
    )
    result = await db.execute(query)
    posts = result.scalars().all()

    items = [
        PostSummary(
            id=p.id,
            title=p.title,
            board_type=p.board_type,
            author_name=p.author_name,  # 관리자 전용 API — 실제 작성자명 표시
            language=p.language or "ko",
            like_count=p.like_count,
            dislike_count=p.dislike_count,
            comment_count=p.comment_count,
            view_count=p.view_count,
            is_pinned=p.is_pinned,
            is_anonymous=p.is_anonymous,
            is_best=(p.like_count - p.dislike_count) >= BEST_POST_THRESHOLD,
            status=p.status,
            created_at=p.created_at,
        )
        for p in posts
    ]

    return PendingPostListResponse(items=items, total=total, page=page, size=size)


@router.get("/posts/{post_id}")
async def get_post(
    post_id: UUID,
    lang: str = Query("ko", description="응답 언어 (ko, en, ja, zh)"),
    db: AsyncSession = Depends(get_db),
    user_id: UUID | None = Depends(get_optional_user_id),
    user_role: str | None = Depends(get_optional_user_role),
):
    """게시글 상세 조회."""
    post = await _get_post_for_visibility(
        db,
        post_id,
        user_role=user_role,
        moderation_read=True,
    )

    # 접근 제어: approved가 아닌 게시글은 admin/moderator 또는 작성자만 접근 가능
    is_admin_or_mod = user_role in MODERATION_ROLES
    is_author = user_id is not None and user_id == post.author_id

    if post.status != PostStatus.APPROVED.value:
        if not (is_admin_or_mod or is_author):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="게시글을 찾을 수 없습니다.")

    # 조회수 증가 (SQL 표현식으로 레이스 컨디션 방지)
    if not post.is_hidden:
        await db.execute(update(Post).where(Post.id == post_id).values(view_count=Post.view_count + 1))
        await db.commit()
        await db.refresh(post)

    # 좋아요/비추천/북마크 상태
    liked = False
    disliked = False
    bookmarked = False
    if user_id:
        like_result = await db.execute(
            select(Like).where(Like.user_id == user_id, Like.post_id == post_id)
        )
        liked = like_result.scalar_one_or_none() is not None

        dislike_result = await db.execute(
            select(Dislike).where(Dislike.user_id == user_id, Dislike.post_id == post_id)
        )
        disliked = dislike_result.scalar_one_or_none() is not None

        bm_result = await db.execute(
            select(Bookmark).where(Bookmark.user_id == user_id, Bookmark.post_id == post_id)
        )
        bookmarked = bm_result.scalar_one_or_none() is not None

    # 익명 글의 작성자명/ID — 관리자에게는 실제 작성자 표시
    display_name = post.author_name
    display_author_id: UUID | None = post.author_id
    if post.is_anonymous and not is_admin_or_mod:
        display_name = "익명"
        display_author_id = None

    # 댓글 조회
    comments_result = await db.execute(
        select(Comment)
        .where(Comment.post_id == post_id, Comment.is_hidden.is_(False))
        .order_by(Comment.created_at.asc(), Comment.id.asc())
    )
    comments_list = [
        {
            "id": str(c.id),
            "author_id": None if (c.is_anonymous and not is_admin_or_mod) or c.is_deleted else str(c.author_id),
            "author_name": "익명" if (c.is_anonymous and not is_admin_or_mod) or c.is_deleted else c.author_name,
            "content": "삭제된 댓글입니다." if c.is_deleted else c.content,
            "parent_id": str(c.parent_id) if c.parent_id else None,
            "is_anonymous": c.is_anonymous,
            "is_deleted": c.is_deleted,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }
        for c in comments_result.scalars().all()
    ]

    resp = PostResponse(
        id=post.id,
        title=_localized(post.title_i18n, post.title, lang),
        content=_localized(post.content_i18n, post.content, lang),
        board_type=post.board_type,
        author_id=display_author_id,
        author_name=display_name,
        challenge_id=post.challenge_id,
        language=post.language or "ko",
        like_count=post.like_count,
        dislike_count=post.dislike_count,
        comment_count=post.comment_count,
        view_count=post.view_count,
        is_pinned=post.is_pinned,
        is_anonymous=post.is_anonymous,
        status=post.status,
        rejection_reason=post.rejection_reason,
        created_at=post.created_at,
        updated_at=post.updated_at,
        liked=liked,
        disliked=disliked,
        bookmarked=bookmarked,
    )
    result_dict = resp.model_dump()
    result_dict["comments"] = comments_list
    return result_dict


@router.post("/posts", status_code=status.HTTP_201_CREATED)
async def create_post(
    body: PostCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
    username: str = Depends(get_current_username),
    user_role: str | None = Depends(get_optional_user_role),
):
    """게시글 작성.

    - free/qna/notice: 자동 승인 (approved) — 바로 공개
    - writeup: 승인 대기 (pending) — 관리자 검토 후 공개
    - 작성 후 백그라운드에서 자동 번역 실행
    """
    # notice는 admin/moderator만 작성 가능
    if body.board_type == "notice" and user_role not in MODERATION_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="공지는 관리자만 작성할 수 있습니다.",
        )

    if not await verify_user_exists(str(user_id)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="auth-service에 등록되지 않은 사용자입니다.",
        )

    # board_type에 따라 초기 상태 결정
    if body.board_type in AUTO_APPROVE_BOARD_TYPES:
        initial_status = PostStatus.APPROVED.value
    elif body.board_type in REQUIRE_APPROVAL_BOARD_TYPES:
        initial_status = PostStatus.PENDING.value
    else:
        initial_status = PostStatus.PENDING.value  # 알 수 없는 유형은 안전하게 대기 처리

    display_name = "익명" if body.is_anonymous else username

    post = Post(
        title=body.title,
        content=body.content,
        board_type=body.board_type,
        author_id=user_id,
        author_name=display_name,
        challenge_id=body.challenge_id,
        is_anonymous=body.is_anonymous,
        language=body.language,
        status=initial_status,
    )
    db.add(post)
    await db.commit()
    await db.refresh(post)

    # 백그라운드 자동 번역
    background_tasks.add_task(
        _run_auto_translate, str(post.id), post.title, post.content, post.language,
    )

    # 상태에 따라 메시지 분기
    if initial_status == PostStatus.APPROVED.value:
        message = "게시글이 작성되었습니다."
    else:
        message = "게시글이 작성되었습니다. 관리자 승인 후 공개됩니다."

    return {"id": str(post.id), "status": post.status, "message": message}


@router.patch("/posts/{post_id}/approve", response_model=ApprovalActionResponse)
async def approve_post(
    post_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """게시글을 승인한다 (admin/moderator 전용)."""
    post = await _get_post_for_visibility(db, post_id)

    if post.status != PostStatus.PENDING.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"현재 상태({post.status})에서는 승인할 수 없습니다. pending 상태에서만 승인 가능합니다.",
        )

    now = datetime.now(timezone.utc)
    post.status = PostStatus.APPROVED.value
    post.rejection_reason = None
    post.reviewed_by = admin_id
    post.reviewed_at = now
    await db.commit()
    await db.refresh(post)

    return ApprovalActionResponse(
        id=post.id,
        status=post.status,
        message="게시글이 승인되었습니다.",
        reviewed_by=post.reviewed_by,
        reviewed_at=post.reviewed_at,
    )


@router.patch("/posts/{post_id}/reject", response_model=ApprovalActionResponse)
async def reject_post(
    post_id: UUID,
    body: PostReject,
    db: AsyncSession = Depends(get_db),
    admin_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """게시글을 거부한다 (admin/moderator 전용). 거부 사유를 반드시 포함해야 한다."""
    post = await _get_post_for_visibility(db, post_id)

    if post.status != PostStatus.PENDING.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"현재 상태({post.status})에서는 거부할 수 없습니다. pending 상태에서만 거부 가능합니다.",
        )

    now = datetime.now(timezone.utc)
    post.status = PostStatus.REJECTED.value
    post.rejection_reason = body.rejection_reason
    post.reviewed_by = admin_id
    post.reviewed_at = now
    await db.commit()
    await db.refresh(post)

    return ApprovalActionResponse(
        id=post.id,
        status=post.status,
        message="게시글이 거부되었습니다.",
        reviewed_by=post.reviewed_by,
        reviewed_at=post.reviewed_at,
    )


@router.patch("/posts/{post_id}/pin")
async def toggle_pin(
    post_id: UUID,
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """게시글 고정/해제 토글 (admin/moderator 전용)."""
    post = await _get_post_for_visibility(db, post_id)

    post.is_pinned = not post.is_pinned
    await db.commit()
    await db.refresh(post)

    action = "고정" if post.is_pinned else "고정 해제"
    return {"is_pinned": post.is_pinned, "message": f"게시글이 {action}되었습니다."}


@router.post("/posts/{post_id}/comments", status_code=status.HTTP_201_CREATED)
async def create_comment(
    post_id: UUID,
    body: CommentCreate,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
    username: str = Depends(get_current_username),
):
    """댓글 작성."""
    if not await verify_user_exists(str(user_id)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="auth-service에 등록되지 않은 사용자입니다.",
        )

    # 게시글 존재 + 승인 상태 확인 (approved가 아닌 게시글에는 댓글 불가)
    post = await _get_post_for_visibility(db, post_id)
    if post.status != PostStatus.APPROVED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="승인되지 않은 게시글에는 댓글을 작성할 수 없습니다.",
        )

    comment_display_name = "익명" if body.is_anonymous else username

    comment = Comment(
        post_id=post_id,
        parent_id=body.parent_id,
        author_id=user_id,
        author_name=comment_display_name,
        content=body.content,
        is_anonymous=body.is_anonymous,
    )
    db.add(comment)
    await db.flush()
    if post.author_id != user_id and not post.is_hidden:
        from app.models.notification_event import CommentNotificationEvent
        db.add(CommentNotificationEvent(recipient_id=post.author_id, post_id=post.id,
                                        comment_id=comment.id))

    # 댓글 수 원자적 증가 (레이스 컨디션 방지)
    await db.execute(
        update(Post).where(Post.id == post_id).values(
            comment_count=Post.comment_count + 1
        )
    )
    await db.commit()
    await db.refresh(comment)

    return {
        "id": str(comment.id),
        "author_id": None if comment.is_anonymous else str(comment.author_id),
        "author_name": comment.author_name,
        "content": comment.content,
        "parent_id": str(comment.parent_id) if comment.parent_id else None,
        "is_anonymous": comment.is_anonymous,
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
    }


@router.post("/posts/{post_id}/like")
async def toggle_like(
    post_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
):
    """좋아요 토글. 비추천이 있으면 자동 해제."""
    post = await _get_post_for_visibility(db, post_id, for_update=True)

    if post.status != PostStatus.APPROVED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="승인되지 않은 게시글에는 좋아요를 할 수 없습니다.",
        )

    existing = await db.execute(
        select(Like).where(Like.user_id == user_id, Like.post_id == post_id)
    )
    like = existing.scalar_one_or_none()

    if like:
        await db.delete(like)
        await db.execute(update(Post).where(Post.id == post_id).values(
            like_count=case((Post.like_count > 0, Post.like_count - 1), else_=0)
        ))
        liked = False
    else:
        # 비추천이 있으면 해제 (상호배제)
        existing_dislike = await db.execute(
            select(Dislike).where(Dislike.user_id == user_id, Dislike.post_id == post_id)
        )
        dislike = existing_dislike.scalar_one_or_none()
        if dislike:
            await db.delete(dislike)
            await db.execute(update(Post).where(Post.id == post_id).values(
                dislike_count=case((Post.dislike_count > 0, Post.dislike_count - 1), else_=0)
            ))

        try:
            db.add(Like(user_id=user_id, post_id=post_id))
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(status_code=409, detail="이미 처리된 요청입니다.")
        await db.execute(update(Post).where(Post.id == post_id).values(like_count=Post.like_count + 1))
        liked = True

    await db.commit()
    await db.refresh(post)
    return {"liked": liked, "like_count": post.like_count, "dislike_count": post.dislike_count}


@router.post("/posts/{post_id}/dislike")
async def toggle_dislike(
    post_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
):
    """비추천 토글. 좋아요가 있으면 자동 해제."""
    post = await _get_post_for_visibility(db, post_id, for_update=True)

    if post.status != PostStatus.APPROVED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="승인되지 않은 게시글에는 비추천을 할 수 없습니다.",
        )

    existing = await db.execute(
        select(Dislike).where(Dislike.user_id == user_id, Dislike.post_id == post_id)
    )
    dislike = existing.scalar_one_or_none()

    if dislike:
        await db.delete(dislike)
        await db.execute(update(Post).where(Post.id == post_id).values(
            dislike_count=case((Post.dislike_count > 0, Post.dislike_count - 1), else_=0)
        ))
        disliked = False
    else:
        # 좋아요가 있으면 해제 (상호배제)
        existing_like = await db.execute(
            select(Like).where(Like.user_id == user_id, Like.post_id == post_id)
        )
        like = existing_like.scalar_one_or_none()
        if like:
            await db.delete(like)
            await db.execute(update(Post).where(Post.id == post_id).values(
                like_count=case((Post.like_count > 0, Post.like_count - 1), else_=0)
            ))

        try:
            db.add(Dislike(user_id=user_id, post_id=post_id))
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(status_code=409, detail="이미 처리된 요청입니다.")
        await db.execute(update(Post).where(Post.id == post_id).values(dislike_count=Post.dislike_count + 1))
        disliked = True

    await db.commit()
    await db.refresh(post)
    return {"disliked": disliked, "like_count": post.like_count, "dislike_count": post.dislike_count}


@router.post("/posts/{post_id}/bookmark")
async def toggle_bookmark(
    post_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
):
    """북마크 토글."""
    await _get_post_for_visibility(db, post_id)

    existing = await db.execute(
        select(Bookmark).where(Bookmark.user_id == user_id, Bookmark.post_id == post_id)
    )
    bookmark = existing.scalar_one_or_none()

    if bookmark:
        await db.delete(bookmark)
        bookmarked = False
    else:
        db.add(Bookmark(user_id=user_id, post_id=post_id))
        bookmarked = True

    await db.commit()
    return {"bookmarked": bookmarked}


# ──── 게시글/댓글 수정·삭제 스키마 ────


class PostUpdate(BaseModel):
    title: str | None = None
    content: str | None = None

    @field_validator("content")
    @classmethod
    def sanitize_content(cls, value: str | None) -> str | None:
        return None if value is None else sanitize_rich_content(value, field_name="게시글 본문")


class CommentUpdate(BaseModel):
    content: str = Field(..., min_length=1)

    @field_validator("content")
    @classmethod
    def sanitize_content(cls, value: str) -> str:
        return sanitize_rich_content(value, field_name="댓글 내용")


# ──── 게시글 수정 (본인만) ────


@router.patch("/posts/{post_id}")
async def update_post(
    post_id: UUID,
    body: PostUpdate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
):
    """게시글 제목/본문 수정 — 작성자 본인만 가능."""
    post = await _get_post_for_visibility(db, post_id)
    if post.author_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="본인의 게시글만 수정할 수 있습니다.")

    changed = False
    source_changed = False
    if body.content is None:
        try:
            canonical_content = normalize_rich_content_for_write(
                post.content,
                field_name="게시글 본문",
            )
            canonical_i18n = normalize_rich_content_mapping(
                post.content_i18n,
                field_name="게시글 본문 번역",
                allow_empty=True,
            )
        except RichContentError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=error.as_detail(),
            ) from error
        if canonical_content != post.content:
            post.content = canonical_content
            changed = True
        if canonical_i18n != (post.content_i18n or {}):
            post.content_i18n = canonical_i18n
            changed = True
    if body.title is not None and body.title != post.title:
        post.title = body.title
        changed = True
        source_changed = True
    if body.content is not None and body.content != post.content:
        post.content = body.content
        changed = True
        source_changed = True

    if changed:
        if source_changed:
            # 기존 i18n 번역 초기화 (수정된 원본이 즉시 표시되도록)
            post.title_i18n = {}
            post.content_i18n = {}
        post.updated_at = datetime.now(timezone.utc)
        await db.commit()

        # 백그라운드에서 자동 번역 재실행
        if source_changed:
            background_tasks.add_task(
                _run_auto_translate,
                str(post.id),
                post.title,
                post.content,
                post.language or "ko",
            )

    return {"message": "게시글이 수정되었습니다."}


# ──── 게시글 삭제 (일반 작성자 본인만) ────


@router.delete("/posts/{post_id}")
async def delete_post(
    post_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
    user_role: str | None = Depends(get_optional_user_role),
):
    """게시글 삭제 — 일반 작성자 본인만 기존 hard-delete 계약을 사용한다.

    관리자·모더레이터는 본인의 게시글이라도 이 경로로 감사·복구 절차를
    우회할 수 없으며, 서버가 409로 차단하고 모더레이션 hide/restore를 강제한다.
    """
    post = await _get_post_for_visibility(db, post_id)
    assert_direct_owner_delete_allowed(user_role, post.author_id == user_id)

    await db.delete(post)
    await db.commit()
    return {"message": "게시글이 삭제되었습니다."}


# ──── 댓글 수정 (본인만) ────


@router.patch("/posts/{post_id}/comments/{comment_id}")
async def update_comment(
    post_id: UUID,
    comment_id: UUID,
    body: CommentUpdate,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
):
    """댓글 수정 — 작성자 본인만 가능."""
    await _get_post_for_visibility(db, post_id)

    result = await db.execute(
        select(Comment).where(Comment.id == comment_id, Comment.post_id == post_id)
    )
    comment = result.scalar_one_or_none()
    if not comment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="댓글을 찾을 수 없습니다.")
    assert_content_visible(
        is_hidden=comment.is_hidden,
        not_found_detail="댓글을 찾을 수 없습니다.",
    )
    if comment.author_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="본인의 댓글만 수정할 수 있습니다.")
    if comment.is_deleted:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="삭제된 댓글은 수정할 수 없습니다.")

    comment.content = body.content
    comment.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(comment)

    return {
        "message": "댓글이 수정되었습니다.",
        "content": comment.content,
        "updated_at": comment.updated_at.isoformat(),
    }


# ──── 댓글 삭제 (일반 작성자 본인만) ────


@router.delete("/posts/{post_id}/comments/{comment_id}")
async def delete_comment(
    post_id: UUID,
    comment_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
    user_role: str | None = Depends(get_optional_user_role),
):
    """댓글 soft delete — 일반 작성자 본인만 기존 soft-delete 계약을 사용한다.

    관리자·모더레이터의 댓글 조치는 모더레이션 확인 토큰과 감사 원장을 거치는
    hide/restore API로만 수행한다.
    """
    await _get_post_for_visibility(db, post_id)

    result = await db.execute(
        select(Comment).where(Comment.id == comment_id, Comment.post_id == post_id)
    )
    comment = result.scalar_one_or_none()
    if not comment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="댓글을 찾을 수 없습니다.")
    assert_content_visible(
        is_hidden=comment.is_hidden,
        not_found_detail="댓글을 찾을 수 없습니다.",
    )
    assert_direct_owner_delete_allowed(user_role, comment.author_id == user_id)

    comment.is_deleted = True
    await db.execute(
        update(Post)
        .where(Post.id == post_id)
        .values(
            comment_count=case(
                (Post.comment_count > 0, Post.comment_count - 1),
                else_=0,
            )
        )
    )
    await db.commit()
    return {"message": "댓글이 삭제되었습니다."}
