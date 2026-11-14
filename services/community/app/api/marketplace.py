"""장터(마켓플레이스) API 라우터."""

import logging
from datetime import datetime, timezone
from uuid import UUID

from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id, get_current_username, get_optional_user_role
from app.api.moderation import assert_direct_owner_delete_allowed
from app.database import get_db
from app.models.marketplace import MarketplaceItem, MarketplacePurchase, MarketplaceComment
from app.utils.auth_client import get_client
from app.utils.marketplace_settlement import (
    TransferGateway, assert_no_pending_purchase, prepare_purchase,
    purchase_status, reconcile_operation,
)
from app.utils.rich_content import RichContentError, normalize_rich_content_for_write
from app.utils.safe_html import sanitize_rich_content
from app.utils.content_visibility import (
    MODERATION_READ_ACCESS,
    PUBLIC_ACCESS,
    assert_content_visible,
)
from app.utils.minio_client import upload_file, get_object_stream, validate_filename, MAX_FILE_SIZE
from app.config import settings

def _validate_uuid(value: str, field_name: str = "ID") -> UUID:
    """문자열을 UUID로 변환한다. 유효하지 않으면 400 에러를 반환한다."""
    try:
        return UUID(value)
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"유효하지 않은 {field_name} 형식입니다.",
        )

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplace", tags=["장터"])


def _normalize_rich_or_422(
    value: str,
    *,
    field_name: str,
    allow_empty: bool,
    max_visible_chars: int,
) -> str:
    try:
        return normalize_rich_content_for_write(
            value,
            field_name=field_name,
            allow_empty=allow_empty,
            max_visible_chars=max_visible_chars,
        )
    except RichContentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.as_detail(),
        ) from exc


def _escape_like(value: str) -> str:
    """LIKE 패턴의 와일드카드 문자를 이스케이프한다."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

# ──── 스키마 ────


class ItemCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    price: int = Field(..., ge=1, le=100000)
    category: str = Field("etc", pattern=r"^(material|etc)$")

    @field_validator("description")
    @classmethod
    def sanitize_description(cls, value: str) -> str:
        return sanitize_rich_content(
            value,
            field_name="상품 설명",
            allow_empty=True,
            max_visible_chars=5000,
        )


class ItemResponse(BaseModel):
    id: str
    title: str
    description: str
    price: int
    category: str
    seller_id: str
    seller_name: str
    status: str
    download_count: int
    original_filename: str | None = None
    created_at: str

    model_config = {"from_attributes": True}


class ItemListResponse(BaseModel):
    items: list[ItemResponse] = []
    total: int = 0
    page: int = 1
    size: int = 20


class MarketCommentCreate(BaseModel):
    content: str
    is_anonymous: bool = False

    @field_validator("content")
    @classmethod
    def sanitize_content(cls, value: str) -> str:
        return sanitize_rich_content(
            value,
            field_name="장터 댓글",
            max_visible_chars=2000,
        )


class MarketCommentResponse(BaseModel):
    id: str
    author_id: str
    author_name: str
    content: str
    is_anonymous: bool
    is_deleted: bool = False
    created_at: str
    updated_at: str | None = None


class PurchaseResponse(BaseModel):
    id: str
    item_id: str
    price_paid: int
    seller_earned: int
    purchased_at: str

    model_config = {"from_attributes": True}


class PurchaseRequest(BaseModel):
    expected_price: int | None = Field(default=None, strict=True, ge=1, le=100000)


# ──── 유틸리티 ────


def _item_to_response(item: MarketplaceItem) -> ItemResponse:
    return ItemResponse(
        id=str(item.id),
        title=item.title,
        description=item.description,
        price=item.price,
        category=item.category,
        seller_id=str(item.seller_id),
        seller_name=item.seller_name,
        status=item.status,
        download_count=item.download_count,
        original_filename=item.original_filename,
        created_at=item.created_at.isoformat() if item.created_at else "",
    )


async def _get_item_for_visibility(
    db: AsyncSession,
    item_id: str,
    *,
    user_role: str | None = None,
    moderation_read: bool = False,
) -> MarketplaceItem:
    """아이템을 조회하고 숨김 아이템의 공개 접근을 서버에서 차단한다."""
    result = await db.execute(
        select(MarketplaceItem).where(MarketplaceItem.id == item_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="아이템을 찾을 수 없습니다.")

    assert_content_visible(
        is_hidden=item.status == "hidden",
        role=user_role,
        scope=MODERATION_READ_ACCESS if moderation_read else PUBLIC_ACCESS,
        not_found_detail="아이템을 찾을 수 없습니다.",
    )
    return item


# ──── 엔드포인트 ────


@router.get("/items", response_model=ItemListResponse)
async def list_items(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    category: str | None = Query(None),
    q: str | None = Query(None, max_length=100),
    db: AsyncSession = Depends(get_db),
):
    """장터 아이템 목록 조회 (검색, 카테고리 필터)."""
    conditions = [MarketplaceItem.status == "active"]
    if category:
        conditions.append(MarketplaceItem.category == category)
    if q:
        escaped_q = _escape_like(q)
        conditions.append(MarketplaceItem.title.ilike(f"%{escaped_q}%", escape="\\"))

    where = and_(*conditions)

    total_q = select(func.count()).select_from(MarketplaceItem).where(where)
    total = (await db.execute(total_q)).scalar() or 0

    items_q = (
        select(MarketplaceItem)
        .where(where)
        .order_by(MarketplaceItem.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    result = await db.execute(items_q)
    items = result.scalars().all()

    return ItemListResponse(
        items=[_item_to_response(item) for item in items],
        total=total,
        page=page,
        size=size,
    )


@router.get("/items/{item_id}", response_model=ItemResponse)
async def get_item(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    user_role: str | None = Depends(get_optional_user_role),
):
    """아이템 상세 조회."""
    _validate_uuid(item_id, "아이템 ID")
    item = await _get_item_for_visibility(
        db,
        item_id,
        user_role=user_role,
        moderation_read=True,
    )
    return _item_to_response(item)


@router.post("/items", response_model=ItemResponse, status_code=201)
async def create_item(
    title: str = Form(..., min_length=1, max_length=200),
    price: int = Form(..., ge=1, le=100000),
    category: str = Form("etc"),
    description: str = Form(""),
    file: UploadFile | None = File(None),
    user_id: UUID = Depends(get_current_user_id),
    username: str = Depends(get_current_username),
    db: AsyncSession = Depends(get_db),
):
    """아이템 등록 (파일 첨부 선택)."""
    if category not in ("exam", "material", "etc"):
        raise HTTPException(status_code=400, detail="유효하지 않은 카테고리입니다.")

    # Validate before uploading a file so rejected content cannot leave an
    # object without a database owner.
    description = _normalize_rich_or_422(
        description,
        field_name="상품 설명",
        allow_empty=True,
        max_visible_chars=5000,
    )

    file_url = None
    original_filename = None

    if file and file.filename:
        # 파일명 보안 검증 (확장자 화이트/블랙리스트, 경로 순회 방지)
        validation_error = validate_filename(file.filename)
        if validation_error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=validation_error,
            )

        file_data = await file.read()
        if len(file_data) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="파일 크기가 50MB를 초과합니다.",
            )
        try:
            file_url = upload_file(
                file_data,
                file.filename,
            )
            original_filename = file.filename
        except Exception:
            logger.error("MinIO 파일 업로드 실패", exc_info=True)
            raise HTTPException(status_code=500, detail="파일 업로드에 실패했습니다.")

    item = MarketplaceItem(
        title=title,
        description=description,
        price=price,
        category=category,
        seller_id=user_id,
        seller_name=username,
        file_url=file_url,
        original_filename=original_filename,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _item_to_response(item)


@router.post("/items/{item_id}/purchase")
async def purchase_item(
    item_id: str,
    response: Response,
    body: PurchaseRequest | None = Body(default=None),
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """영속 구매 작업을 먼저 저장하고 Auth 원장으로 결제와 권한을 확정한다."""
    parsed_id = _validate_uuid(item_id, "아이템 ID")
    await _get_item_for_visibility(db, item_id)
    result = await prepare_purchase(db, parsed_id, user_id, expected_price=body.expected_price if body else None)
    if result["status"] == "processing":
        try:
            result = await reconcile_operation(
                db, UUID(result["operation_id"]), TransferGateway(get_client(), settings.auth_service_url),
            )
        except Exception as error:
            await db.rollback()
            logger.warning("영속 구매 작업의 정산을 보류했습니다: %s", type(error).__name__)
    if result["status"] == "processing":
        response.status_code = status.HTTP_202_ACCEPTED
    elif result["status"] == "rejected":
        response.status_code = status.HTTP_400_BAD_REQUEST
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/items/{item_id}/purchase-status")
async def get_purchase_status(
    item_id: str,
    response: Response,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """자신의 구매 상태만 읽는다. 원격 쓰기는 구매 POST와 worker가 수행한다."""
    parsed_id = _validate_uuid(item_id, "아이템 ID")
    result = await purchase_status(db, parsed_id, user_id)
    if result["status"] == "not_started":
        await _get_item_for_visibility(db, item_id)
    if result["status"] == "processing":
        response.status_code = status.HTTP_202_ACCEPTED
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/items/{item_id}/purchased")
async def check_purchased(
    item_id: str,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """아이템 구매 여부 확인."""
    _validate_uuid(item_id, "아이템 ID")
    await _get_item_for_visibility(db, item_id)

    result = await db.execute(
        select(MarketplacePurchase).where(
            MarketplacePurchase.item_id == item_id,
            MarketplacePurchase.buyer_id == user_id,
        )
    )
    purchased = result.scalar_one_or_none() is not None
    return {"purchased": purchased}


@router.get("/my/purchases")
async def my_purchases(
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """내 구매 내역 — JOIN으로 한 번에 조회."""
    from sqlalchemy.orm import aliased

    item_alias = aliased(MarketplaceItem)
    result = await db.execute(
        select(MarketplacePurchase, item_alias.title)
        .outerjoin(item_alias, MarketplacePurchase.item_id == item_alias.id)
        .where(
            MarketplacePurchase.buyer_id == user_id,
            or_(
                item_alias.id.is_(None),
                item_alias.status != "hidden",
            ),
        )
        .order_by(MarketplacePurchase.purchased_at.desc())
    )
    rows = result.all()

    return [
        {
            "purchase_id": str(p.id),
            "item_id": str(p.item_id),
            "item_title": item_title if item_title else "(삭제된 아이템)",
            "price_paid": p.price_paid,
            "purchased_at": p.purchased_at.isoformat() if p.purchased_at else "",
        }
        for p, item_title in rows
    ]


@router.get("/my/items")
async def my_items(
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """내가 등록한 아이템 목록."""
    result = await db.execute(
        select(MarketplaceItem)
        .where(
            MarketplaceItem.seller_id == user_id,
            MarketplaceItem.status != "hidden",
        )
        .order_by(MarketplaceItem.created_at.desc())
    )
    items = result.scalars().all()
    return [_item_to_response(item) for item in items]


@router.get("/items/{item_id}/download")
async def download_item(
    item_id: str,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """아이템 파일 다운로드 — 구매자 또는 판매자만 가능."""
    _validate_uuid(item_id, "아이템 ID")
    # 아이템 조회
    item = await _get_item_for_visibility(db, item_id)

    if not item.file_url:
        raise HTTPException(status_code=404, detail="첨부 파일이 없는 아이템입니다.")

    # 판매자 본인이면 바로 다운로드 허용
    is_seller = item.seller_id == user_id

    if not is_seller:
        # 구매 여부 확인
        purchase = await db.execute(
            select(MarketplacePurchase).where(
                MarketplacePurchase.item_id == item.id,
                MarketplacePurchase.buyer_id == user_id,
            )
        )
        if not purchase.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="구매 후 다운로드할 수 있습니다.")

    # MinIO에서 직접 스트리밍 (presigned URL의 127.0.0.1 문제 방지)
    try:
        stream, content_type, size = get_object_stream(item.file_url)
    except Exception:
        logger.error("MinIO 파일 다운로드 실패: %s", item.file_url, exc_info=True)
        raise HTTPException(status_code=500, detail="파일 다운로드에 실패했습니다.")

    filename = item.original_filename or "download"
    encoded_filename = quote(filename)

    return StreamingResponse(
        stream,
        media_type=content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
            "Content-Length": str(size),
        },
    )


@router.get("/items/{item_id}/comments")
async def list_item_comments(
    item_id: str,
    db: AsyncSession = Depends(get_db),
):
    """아이템 댓글 목록 조회."""
    _validate_uuid(item_id, "아이템 ID")
    await _get_item_for_visibility(db, item_id)

    result = await db.execute(
        select(MarketplaceComment)
        .where(
            MarketplaceComment.item_id == item_id,
            MarketplaceComment.is_hidden.is_(False),
        )
        .order_by(MarketplaceComment.created_at.asc())
    )
    comments = result.scalars().all()
    return [
        MarketCommentResponse(
            id=str(c.id),
            author_id=str(c.author_id),
            author_name="익명" if c.is_anonymous else c.author_name,
            content="삭제된 댓글입니다." if c.is_deleted else c.content,
            is_anonymous=c.is_anonymous,
            is_deleted=c.is_deleted if c.is_deleted else False,
            created_at=c.created_at.isoformat() if c.created_at else "",
            updated_at=c.updated_at.isoformat() if c.updated_at else None,
        )
        for c in comments
    ]


@router.post("/items/{item_id}/comments", status_code=201)
async def create_item_comment(
    item_id: str,
    body: MarketCommentCreate,
    user_id: UUID = Depends(get_current_user_id),
    username: str = Depends(get_current_username),
    db: AsyncSession = Depends(get_db),
):
    """아이템에 댓글 작성."""
    _validate_uuid(item_id, "아이템 ID")
    await _get_item_for_visibility(db, item_id)

    comment = MarketplaceComment(
        item_id=item_id,
        author_id=user_id,
        author_name=username,
        content=body.content,
        is_anonymous=body.is_anonymous,
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)

    return MarketCommentResponse(
        id=str(comment.id),
        author_id=str(comment.author_id),
        author_name="익명" if comment.is_anonymous else comment.author_name,
        content=comment.content,
        is_anonymous=comment.is_anonymous,
        created_at=comment.created_at.isoformat() if comment.created_at else "",
    )


# ──── 수정·삭제 스키마 ────


class ItemUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    price: int | None = None

    @field_validator("description")
    @classmethod
    def sanitize_description(cls, value: str | None) -> str | None:
        return None if value is None else sanitize_rich_content(
            value,
            field_name="상품 설명",
            allow_empty=True,
            max_visible_chars=5000,
        )


class MarketCommentUpdate(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def sanitize_content(cls, value: str) -> str:
        return sanitize_rich_content(
            value,
            field_name="장터 댓글",
            max_visible_chars=2000,
        )


# ──── 아이템 수정·삭제 ────


@router.patch("/items/{item_id}")
async def update_item(
    item_id: str,
    body: ItemUpdate,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """장터 아이템 수정 (본인만)."""
    _validate_uuid(item_id, "아이템 ID")
    item = await _get_item_for_visibility(db, item_id)
    if item.seller_id != user_id:
        raise HTTPException(status_code=403, detail="본인의 아이템만 수정할 수 있습니다.")

    canonical_description = _normalize_rich_or_422(
        body.description if body.description is not None else item.description,
        field_name="상품 설명",
        allow_empty=True,
        max_visible_chars=5000,
    )

    if body.title is not None:
        item.title = body.title
    item.description = canonical_description
    if body.price is not None:
        item.price = body.price

    await db.commit()
    return {"message": "아이템이 수정되었습니다."}


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: str,
    user_id: UUID = Depends(get_current_user_id),
    user_role: str | None = Depends(get_optional_user_role),
    db: AsyncSession = Depends(get_db),
):
    """장터 아이템 삭제 — 일반 판매자 본인만 기존 hard-delete 계약을 사용한다.

    관리자·모더레이터는 판매자 본인인 경우에도 이 직접 삭제 경로를 사용할 수
    없으며, 확인 토큰과 감사 원장을 갖는 모더레이션 hide/restore를 사용한다.
    """
    _validate_uuid(item_id, "아이템 ID")
    item = await _get_item_for_visibility(db, item_id)

    assert_direct_owner_delete_allowed(user_role, item.seller_id == user_id)

    await db.execute(select(MarketplaceItem).where(MarketplaceItem.id == item.id).with_for_update())
    await assert_no_pending_purchase(db, item.id)

    await db.delete(item)
    await db.commit()
    return {"message": "아이템이 삭제되었습니다."}


# ──── 댓글 수정·삭제 ────


@router.patch("/items/{item_id}/comments/{comment_id}")
async def update_item_comment(
    item_id: str,
    comment_id: str,
    body: MarketCommentUpdate,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """장터 댓글 수정 (본인만)."""
    _validate_uuid(item_id, "아이템 ID")
    _validate_uuid(comment_id, "댓글 ID")
    await _get_item_for_visibility(db, item_id)

    result = await db.execute(
        select(MarketplaceComment).where(
            MarketplaceComment.id == comment_id,
            MarketplaceComment.item_id == item_id,
        )
    )
    comment = result.scalar_one_or_none()
    if not comment:
        raise HTTPException(status_code=404, detail="댓글을 찾을 수 없습니다.")
    assert_content_visible(
        is_hidden=comment.is_hidden,
        not_found_detail="댓글을 찾을 수 없습니다.",
    )
    if comment.author_id != user_id:
        raise HTTPException(status_code=403, detail="본인의 댓글만 수정할 수 있습니다.")
    if comment.is_deleted:
        raise HTTPException(status_code=400, detail="삭제된 댓글은 수정할 수 없습니다.")

    comment.content = body.content
    comment.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(comment)

    return {
        "message": "댓글이 수정되었습니다.",
        "content": comment.content,
        "updated_at": comment.updated_at.isoformat(),
    }


@router.delete("/items/{item_id}/comments/{comment_id}")
async def delete_item_comment(
    item_id: str,
    comment_id: str,
    user_id: UUID = Depends(get_current_user_id),
    user_role: str | None = Depends(get_optional_user_role),
    db: AsyncSession = Depends(get_db),
):
    """장터 댓글 삭제 — 일반 작성자 본인만 기존 soft-delete 계약을 사용한다.

    관리자·모더레이터의 조치는 직접 삭제 대신 모더레이션 hide/restore API로
    강제되어 3단계 확인과 감사 기록을 우회할 수 없다.
    """
    _validate_uuid(item_id, "아이템 ID")
    _validate_uuid(comment_id, "댓글 ID")
    await _get_item_for_visibility(db, item_id)

    result = await db.execute(
        select(MarketplaceComment).where(
            MarketplaceComment.id == comment_id,
            MarketplaceComment.item_id == item_id,
        )
    )
    comment = result.scalar_one_or_none()
    if not comment:
        raise HTTPException(status_code=404, detail="댓글을 찾을 수 없습니다.")
    assert_content_visible(
        is_hidden=comment.is_hidden,
        not_found_detail="댓글을 찾을 수 없습니다.",
    )

    assert_direct_owner_delete_allowed(user_role, comment.author_id == user_id)

    comment.is_deleted = True
    await db.commit()
    return {"message": "댓글이 삭제되었습니다."}
