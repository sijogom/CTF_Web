"""플랫폼 공개 통계 API.

메인 페이지의 LiveStats 컴포넌트에서 사용한다.
인증 없이 접근 가능한 공개 엔드포인트.
"""

import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.competition import Competition

logger = logging.getLogger(__name__)
router = APIRouter(tags=["통계"])

LEARN_URL = settings.learn_service_url


@router.get("/stats/platform")
async def platform_stats(db: AsyncSession = Depends(get_db)):
    """플랫폼 전체 통계 (공개)."""
    # 활성 사용자 수
    user_count = await db.execute(
        select(func.count()).select_from(User).where(User.is_active.is_(True))
    )
    users = user_count.scalar() or 0

    # 공개 대회 목록과 같은 경계: 시작 시각 포함, 종료 시각 제외.
    now = datetime.now(timezone.utc)
    comp_count = await db.execute(
        select(func.count()).select_from(Competition).where(
            Competition.is_deleted.is_(False),
            Competition.is_published.is_(True),
            Competition.start_at <= now,
            Competition.end_at > now,
        )
    )
    competitions = comp_count.scalar() or 0

    # learn-service에서 문제/풀이 통계 조회
    challenges = 0
    solves = 0
    internal_headers = {"X-Internal-Key": settings.internal_api_key} if settings.internal_api_key else {}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{LEARN_URL}/internal/stats", headers=internal_headers)
            if resp.status_code == 200:
                data = resp.json()
                challenges = data.get("total_challenges", 0)
                solves = data.get("total_solves", 0)
    except Exception as exc:
        logger.warning("learn-service 통계 조회 실패: %s", exc)

    return {
        "users": users,
        "challenges": challenges,
        "solves": solves,
        "competitions": competitions,
    }
