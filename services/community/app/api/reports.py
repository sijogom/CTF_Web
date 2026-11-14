"""신고 API."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.post import Report, Post, Comment
from app.api.deps import get_current_user_id, require_role

router = APIRouter(tags=["신고"])


# ──── 스키마 ────

class ReportCreate(BaseModel):
    target_type: str = Field(..., pattern=r"^(post|comment|marketplace_item|marketplace_comment)$")
    target_id: UUID
    reason: str = Field(..., pattern=r"^(spam|abuse|inappropriate|other)$")
    detail: str | None = None


class ReportStatusUpdate(BaseModel):
    """신고 상태 변경 요청 바디."""
    status: str = Field(..., pattern=r"^(pending|reviewed|dismissed)$")


class ReportResponse(BaseModel):
    id: UUID
    reporter_id: UUID
    target_type: str
    target_id: UUID
    reason: str
    detail: str | None
    status: str
    reviewed_by: UUID | None = None
    created_at: datetime
    # 신고 대상 콘텐츠 요약 (관리자 확인용)
    target_title: str | None = None
    target_preview: str | None = None
    target_author: str | None = None
    target_deleted: bool = False
    target_hidden: bool = False

    model_config = {"from_attributes": True}


class ReportListResponse(BaseModel):
    """페이지네이션 포함 신고 목록 응답."""
    items: list[ReportResponse]
    total: int
    page: int
    size: int


# ──── 엔드포인트 ────

@router.post("/reports", response_model=ReportResponse, status_code=201)
async def create_report(
    body: ReportCreate,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
):
    """신고 접수."""
    # 대상 존재 확인
    if body.target_type == "post":
        result = await db.execute(select(Post).where(Post.id == body.target_id))
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="신고 대상 게시글을 찾을 수 없습니다.")
    elif body.target_type == "comment":
        result = await db.execute(select(Comment).where(Comment.id == body.target_id))
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="신고 대상 댓글을 찾을 수 없습니다.")
    elif body.target_type == "marketplace_item":
        from app.models.marketplace import MarketplaceItem
        result = await db.execute(select(MarketplaceItem).where(MarketplaceItem.id == body.target_id))
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="신고 대상 아이템을 찾을 수 없습니다.")
    elif body.target_type == "marketplace_comment":
        from app.models.marketplace import MarketplaceComment
        result = await db.execute(select(MarketplaceComment).where(MarketplaceComment.id == body.target_id))
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="신고 대상 댓글을 찾을 수 없습니다.")

    # 중복 신고 방지
    existing = await db.execute(
        select(Report).where(
            Report.reporter_id == user_id,
            Report.target_type == body.target_type,
            Report.target_id == body.target_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="이미 신고한 대상입니다.")

    report = Report(
        reporter_id=user_id,
        target_type=body.target_type,
        target_id=body.target_id,
        reason=body.reason,
        detail=body.detail,
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)

    return ReportResponse(
        id=report.id,
        reporter_id=report.reporter_id,
        target_type=report.target_type,
        target_id=report.target_id,
        reason=report.reason,
        detail=report.detail,
        status=report.status,
        created_at=report.created_at,
        target_hidden=await _is_report_target_hidden(db, report.target_type, report.target_id),
    )


@router.get("/reports", response_model=ReportListResponse)
async def list_reports(
    status_filter: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """신고 목록 (관리자 전용)."""
    # 공통 필터 조건
    base_filters = []
    if status_filter:
        base_filters.append(Report.status == status_filter)

    # 총 개수 쿼리 — 필터 동일 적용
    count_query = select(func.count()).select_from(Report)
    for f in base_filters:
        count_query = count_query.where(f)
    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    # 데이터 쿼리
    query = select(Report).order_by(Report.created_at.desc())
    for f in base_filters:
        query = query.where(f)
    query = query.offset((page - 1) * size).limit(size)

    result = await db.execute(query)
    reports = result.scalars().all()

    # 신고 대상 콘텐츠 정보를 일괄 조회
    items = []
    for r in reports:
        title, preview, author, deleted = await _resolve_target_content(db, r.target_type, r.target_id)
        items.append(ReportResponse(
            id=r.id,
            reporter_id=r.reporter_id,
            target_type=r.target_type,
            target_id=r.target_id,
            reason=r.reason,
            detail=r.detail,
            status=r.status,
            created_at=r.created_at,
            target_title=title,
            target_preview=preview,
            target_author=author,
            target_deleted=deleted,
            target_hidden=await _is_report_target_hidden(db, r.target_type, r.target_id),
        ))

    return ReportListResponse(items=items, total=total, page=page, size=size)


@router.patch("/reports/{report_id}")
async def update_report_status(
    report_id: UUID,
    body: ReportStatusUpdate,
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
    reviewer_id: UUID = Depends(get_current_user_id),
):
    """신고 상태 변경 (관리자 전용)."""
    result = await db.execute(select(Report).where(Report.id == report_id))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="신고를 찾을 수 없습니다.")

    if body.status == "reviewed":
        target_hidden = await _is_report_target_hidden(db, report.target_type, report.target_id)
        target_non_actionable = await _is_report_target_non_actionable(
            db,
            report.target_type,
            report.target_id,
        )
        if not target_hidden and not target_non_actionable:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="숨김 처리에는 모더레이션 확인 토큰이 필요합니다.",
            )

    report.status = body.status
    report.reviewed_by = reviewer_id  # 처리한 관리자/모더레이터 기록

    await db.commit()
    return {"message": "신고 상태가 변경되었습니다.", "status": body.status}


async def _resolve_target_content(
    db: AsyncSession,
    target_type: str,
    target_id: UUID,
) -> tuple[str | None, str | None, str | None, bool]:
    """신고 대상의 (title, preview, author, deleted) 반환."""
    if target_type == "post":
        result = await db.execute(select(Post).where(Post.id == target_id))
        post = result.scalar_one_or_none()
        if not post:
            return None, None, None, True
        return post.title, (post.content or "")[:100], post.author_name, False

    elif target_type == "comment":
        result = await db.execute(select(Comment).where(Comment.id == target_id))
        comment = result.scalar_one_or_none()
        if not comment:
            return None, None, None, True
        return None, (comment.content or "")[:100], comment.author_name, comment.is_deleted

    elif target_type == "marketplace_item":
        from app.models.marketplace import MarketplaceItem
        result = await db.execute(select(MarketplaceItem).where(MarketplaceItem.id == target_id))
        item = result.scalar_one_or_none()
        if not item:
            return None, None, None, True
        return item.title, (item.description or "")[:100], item.seller_name, False

    elif target_type == "marketplace_comment":
        from app.models.marketplace import MarketplaceComment
        result = await db.execute(select(MarketplaceComment).where(MarketplaceComment.id == target_id))
        comment = result.scalar_one_or_none()
        if not comment:
            return None, None, None, True
        return None, (comment.content or "")[:100], comment.author_name, getattr(comment, "is_deleted", False)

    return None, None, None, True


async def _is_report_target_hidden(
    db: AsyncSession,
    target_type: str,
    target_id: UUID,
) -> bool:
    """신고 대상이 현재 숨김 상태인지 확인한다."""
    if target_type == "post":
        result = await db.execute(select(Post).where(Post.id == target_id))
        target = result.scalar_one_or_none()
        return bool(target and target.is_hidden)

    elif target_type == "comment":
        result = await db.execute(select(Comment).where(Comment.id == target_id))
        target = result.scalar_one_or_none()
        return bool(target and target.is_hidden)

    elif target_type == "marketplace_item":
        from app.models.marketplace import MarketplaceItem
        result = await db.execute(select(MarketplaceItem).where(MarketplaceItem.id == target_id))
        target = result.scalar_one_or_none()
        return bool(target and target.status == "hidden")

    elif target_type == "marketplace_comment":
        from app.models.marketplace import MarketplaceComment
        result = await db.execute(select(MarketplaceComment).where(MarketplaceComment.id == target_id))
        target = result.scalar_one_or_none()
        return bool(target and target.is_hidden)

    return False


async def _is_report_target_non_actionable(
    db: AsyncSession,
    target_type: str,
    target_id: UUID,
) -> bool:
    """삭제됐거나 이미 존재하지 않아 가시성 변경을 할 수 없는 신고 대상인지 확인한다."""
    if target_type == "post":
        result = await db.execute(select(Post).where(Post.id == target_id))
        return result.scalar_one_or_none() is None

    if target_type == "comment":
        result = await db.execute(select(Comment).where(Comment.id == target_id))
        target = result.scalar_one_or_none()
        return target is None or bool(target.is_deleted)

    if target_type == "marketplace_item":
        from app.models.marketplace import MarketplaceItem

        result = await db.execute(select(MarketplaceItem).where(MarketplaceItem.id == target_id))
        return result.scalar_one_or_none() is None

    if target_type == "marketplace_comment":
        from app.models.marketplace import MarketplaceComment

        result = await db.execute(select(MarketplaceComment).where(MarketplaceComment.id == target_id))
        target = result.scalar_one_or_none()
        return target is None or bool(target.is_deleted)

    return False
