"""내부 서비스용 API — auth-service에서 뱃지 데이터 조회 시 사용."""

import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.post import Post, Comment

logger = logging.getLogger(__name__)


def verify_internal_key(request: Request) -> None:
    """X-Internal-Key 헤더를 검증한다. 키가 설정되어 있으면 반드시 일치해야 한다."""
    if not settings.internal_api_key:
        if settings.is_production:
            raise HTTPException(status_code=503, detail="내부 API 키 미설정")
        logger.warning(
            "INTERNAL_API_KEY 미설정 — 개발 환경에서 내부 API 인증을 건너뜁니다. (path=%s)",
            request.url.path,
        )
        return  # 개발 환경에서 키 미설정 시 통과
    key = request.headers.get("X-Internal-Key", "")
    if not hmac.compare_digest(key, settings.internal_api_key):
        raise HTTPException(status_code=403, detail="내부 API 인증 실패")


router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(verify_internal_key)])


@router.get("/users/{user_id}/badge-stats")
async def user_badge_stats(user_id: str, db: AsyncSession = Depends(get_db)):
    """커뮤니티 뱃지 통계 (auth-service 뱃지 평가용).

    반환:
        post_count: 승인된 게시글 수 (draft/pending/rejected/숨김 제외)
        max_like_count: 승인된 게시글 중 최다 좋아요 수
        helpful_count: 타인의 QnA 게시글에 작성한 답변(댓글) 수
    """
    # 승인된 게시글만 카운트 (draft/pending/rejected 제외)
    approved_filter = [
        Post.author_id == user_id,
        Post.is_hidden.is_(False),
        Post.status == "approved",
    ]

    # 게시글 수
    post_count_result = await db.execute(
        select(func.count())
        .select_from(Post)
        .where(*approved_filter)
    )
    post_count = post_count_result.scalar() or 0

    # 최다 좋아요 수
    max_like_result = await db.execute(
        select(func.coalesce(func.max(Post.like_count), 0))
        .where(*approved_filter)
    )
    max_like_count = max_like_result.scalar() or 0

    # 도움 활동 수: 다른 사람의 QnA 게시글에 작성한 댓글 수 (숨김 제외)
    helpful_result = await db.execute(
        select(func.count())
        .select_from(Comment)
        .join(Post, Post.id == Comment.post_id)
        .where(
            Comment.author_id == user_id,
            Comment.is_hidden.is_(False),
            Post.author_id != user_id,          # 본인 글 제외
            Post.board_type == "qna",           # QnA 게시판만
            Post.status == "approved",
        )
    )
    helpful_count = helpful_result.scalar() or 0

    return {
        "post_count": post_count,
        "max_like_count": max_like_count,
        "helpful_count": helpful_count,
    }
