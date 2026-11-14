import json
import logging

import httpx
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["랭킹"])

LEARN_URL = settings.learn_service_url


def _internal_headers() -> dict[str, str]:
    headers = {}
    if settings.internal_api_key:
        headers["X-Internal-Key"] = settings.internal_api_key
    return headers


VALID_CATEGORIES = {"web", "crypto", "pwn", "rev", "forensic", "misc"}


async def _fetch_user_solves(
    user_ids: list[str],
    category: str | None = None,
) -> dict[str, int]:
    """learn-service에서 사용자별 풀이 수를 일괄 조회.

    category가 지정되면 해당 카테고리 문제의 풀이 수만 반환한다.
    """
    if not user_ids:
        return {}
    try:
        payload: dict = {"user_ids": user_ids}
        if category:
            payload["category"] = category
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{LEARN_URL}/internal/users/solves",
                json=payload,
                headers=_internal_headers(),
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as exc:
        logger.warning("learn-service 풀이 수 조회 실패: %s", exc)
    return {}


@router.get("/rankings")
async def get_rankings(
    tab: str = Query("overall"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """활성 유저 랭킹 목록 (관리자 제외).

    tab="overall" → XP 내림차순 정렬 (기존 동작).
    tab=카테고리(web, crypto 등) → 해당 카테고리 풀이 수 내림차순 정렬.
    """
    category = tab if tab in VALID_CATEGORIES else None

    # 티어 목록 로드
    tiers_list = []
    try:
        from app.redis import redis_client
        raw = await redis_client.get("admin:settings")
        if raw:
            tiers_list = sorted(json.loads(raw).get("tiers") or [], key=lambda t: t.get("required_xp", 0))
    except Exception:
        pass

    def _match_tier(xp: int, tier_override: str | None = None) -> dict | None:
        if tier_override:
            for tier in tiers_list:
                if tier.get("name") == tier_override:
                    return tier
        matched = None
        for tier in tiers_list:
            if xp >= tier.get("required_xp", 0):
                matched = tier
        return matched

    if category:
        # 카테고리별 랭킹: 모든 활성 유저를 가져온 뒤 풀이 수로 정렬
        # 풀이 수가 0인 유저는 제외하여 의미 있는 랭킹만 표시
        all_query = (
            select(User)
            .where(User.is_active == True)  # noqa: E712
            .where(User.role.notin_(["admin", "moderator"]))
        )
        result = await db.execute(all_query)
        all_users = result.scalars().all()

        user_ids = [str(u.id) for u in all_users]
        solves_map = await _fetch_user_solves(user_ids, category=category)

        # 풀이 수 > 0인 유저만 필터 후 풀이 수 내림차순, 동률이면 XP 내림차순
        users_with_solves = [
            (u, solves_map.get(str(u.id), 0))
            for u in all_users
            if solves_map.get(str(u.id), 0) > 0
        ]
        users_with_solves.sort(key=lambda x: (x[1], x[0].xp), reverse=True)

        # 페이지네이션
        start = (page - 1) * size
        page_slice = users_with_solves[start : start + size]

        return [
            {
                "rank": start + i + 1,
                "user_id": str(u.id),
                "username": u.username,
                "avatar_url": u.avatar_url,
                "level": u.level,
                "xp": u.xp,
                "solves": solves,
                "streak": u.streak_days,
                "discord_username": u.discord_username,
                "baekjoon_handle": u.baekjoon_handle,
                "baekjoon_tier": u.baekjoon_tier,
                "tier_name": (_match_tier(u.xp, u.tier_override) or {}).get("name"),
                "tier_image": (_match_tier(u.xp, u.tier_override) or {}).get("image_url"),
            }
            for i, (u, solves) in enumerate(page_slice)
        ]
    else:
        # overall: XP 내림차순 (기존 동작)
        query = (
            select(User)
            .where(User.is_active == True)  # noqa: E712
            .where(User.role.notin_(["admin", "moderator"]))
            .order_by(desc(User.xp))
        )
        query = query.offset((page - 1) * size).limit(size)
        result = await db.execute(query)
        users = result.scalars().all()

        user_ids = [str(u.id) for u in users]
        solves_map = await _fetch_user_solves(user_ids)

        return [
            {
                "rank": (page - 1) * size + i + 1,
                "user_id": str(u.id),
                "username": u.username,
                "avatar_url": u.avatar_url,
                "level": u.level,
                "xp": u.xp,
                "solves": solves_map.get(str(u.id), 0),
                "streak": u.streak_days,
                "discord_username": u.discord_username,
                "baekjoon_handle": u.baekjoon_handle,
                "baekjoon_tier": u.baekjoon_tier,
                "tier_name": (_match_tier(u.xp, u.tier_override) or {}).get("name"),
                "tier_image": (_match_tier(u.xp, u.tier_override) or {}).get("image_url"),
            }
            for i, u in enumerate(users)
        ]
