"""인증 없이 노출해도 되는 사이트 설정만 제공한다."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.utils.admin_settings import (
    ADMIN_SETTINGS_UNAVAILABLE_DETAIL,
    AdminSettingsStorageError,
    default_admin_settings,
    load_admin_settings,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["공개 설정"])


class PublicSettingsResponse(BaseModel):
    site_name: str


def _public_site_name(value: object) -> str:
    """기존 저장값이 손상돼도 공개 응답을 작고 안정적인 문자열로 제한한다."""
    fallback = str(default_admin_settings()["site_name"])
    if not isinstance(value, str):
        return fallback
    normalized = value.strip()
    if not normalized or len(normalized) > 100:
        return fallback
    return normalized


@router.get("/settings/public", response_model=PublicSettingsResponse)
async def get_public_settings(
    db: AsyncSession = Depends(get_db),
) -> PublicSettingsResponse:
    """Header 등 익명 화면이 쓰는 공개 allowlist를 반환한다."""
    try:
        # 익명 표시용 조회는 PostgreSQL 기준값만 읽는다. 공개 페이지 방문이
        # Redis 캐시를 갱신하는 쓰기 동작이나 가용성 의존성을 만들지 않는다.
        settings, _source = await load_admin_settings(db, refresh_cache=False)
    except AdminSettingsStorageError as exc:
        logger.warning("공개 사이트 설정 조회 실패: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ADMIN_SETTINGS_UNAVAILABLE_DETAIL,
        ) from exc
    return PublicSettingsResponse(site_name=_public_site_name(settings.get("site_name")))
