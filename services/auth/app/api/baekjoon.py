"""백준(solved.ac) 연동 API — 인증 코드 검증 + 티어 조회."""

import logging
import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.api.deps import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["백준"])

SOLVED_AC_API = "https://solved.ac/api/v3"

TIER_NAMES = [
    "Unrated",
    "Bronze V", "Bronze IV", "Bronze III", "Bronze II", "Bronze I",
    "Silver V", "Silver IV", "Silver III", "Silver II", "Silver I",
    "Gold V", "Gold IV", "Gold III", "Gold II", "Gold I",
    "Platinum V", "Platinum IV", "Platinum III", "Platinum II", "Platinum I",
    "Diamond V", "Diamond IV", "Diamond III", "Diamond II", "Diamond I",
    "Ruby V", "Ruby IV", "Ruby III", "Ruby II", "Ruby I",
    "Master",
]


def tier_name(tier: int | None) -> str:
    if tier is None or tier < 0 or tier >= len(TIER_NAMES):
        return "Unrated"
    return TIER_NAMES[tier]


async def _fetch_solved_ac_user(handle: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{SOLVED_AC_API}/user/show", params={"handle": handle})
            if resp.status_code == 200:
                return resp.json()
            return None
    except Exception as exc:
        logger.warning("solved.ac API 조회 실패 (handle=%s): %s", handle, exc)
        return None


# ── 인증 코드 임시 저장 (Redis) ──

async def _store_verify_code(user_id: str, handle: str, code: str) -> None:
    from app.redis import redis_client
    key = f"bj_verify:{user_id}"
    import json
    await redis_client.set(key, json.dumps({"handle": handle, "code": code}), ex=600)  # 10분


async def _get_verify_code(user_id: str) -> dict | None:
    from app.redis import redis_client
    import json
    raw = await redis_client.get(f"bj_verify:{user_id}")
    if raw:
        return json.loads(raw)
    return None


async def _delete_verify_code(user_id: str) -> None:
    from app.redis import redis_client
    await redis_client.delete(f"bj_verify:{user_id}")


# ── API ──


class StartLinkRequest(BaseModel):
    handle: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-zA-Z0-9_]+$")


@router.post("/baekjoon/start-link")
async def start_link(
    body: StartLinkRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """1단계: 백준 아이디 입력 → 인증 코드 발급."""
    handle = body.handle.strip()

    solved_user = await _fetch_solved_ac_user(handle)
    if not solved_user:
        raise HTTPException(status_code=404, detail=f"'{handle}' 사용자를 찾을 수 없습니다.")

    existing = await db.execute(
        select(User.id).where(User.baekjoon_handle == handle, User.id != user.id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="이 백준 아이디는 이미 다른 계정에 연결되어 있습니다.")

    code = f"CUHA-{secrets.token_hex(4).upper()}"
    await _store_verify_code(str(user.id), handle, code)

    return {
        "handle": handle,
        "code": code,
        "message": f"solved.ac 프로필 자기소개(Bio)에 '{code}'를 추가한 후 '인증 확인' 버튼을 눌러주세요.",
    }


@router.post("/baekjoon/verify")
async def verify_link(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """2단계: solved.ac Bio에 인증 코드가 있는지 확인 → 연동 완료."""
    pending = await _get_verify_code(str(user.id))
    if not pending:
        raise HTTPException(status_code=400, detail="인증 요청이 없거나 만료되었습니다. 다시 시도해주세요.")

    handle = pending["handle"]
    code = pending["code"]

    solved_user = await _fetch_solved_ac_user(handle)
    if not solved_user:
        raise HTTPException(status_code=502, detail="solved.ac에서 정보를 조회할 수 없습니다.")

    bio = solved_user.get("bio", "") or ""
    if code not in bio:
        raise HTTPException(
            status_code=400,
            detail=f"solved.ac 자기소개에 인증 코드 '{code}'가 없습니다. 추가 후 다시 시도해주세요.",
        )

    tier = solved_user.get("tier", 0)
    user.baekjoon_handle = handle
    user.baekjoon_tier = tier
    await db.commit()
    await _delete_verify_code(str(user.id))

    return {
        "handle": handle,
        "tier": tier,
        "tier_name": tier_name(tier),
        "message": "백준 연동이 완료되었습니다!",
    }


@router.post("/baekjoon/unlink")
async def unlink_baekjoon(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user.baekjoon_handle = None
    user.baekjoon_tier = None
    await db.commit()
    return {"message": "백준 연동이 해제되었습니다."}


@router.get("/baekjoon/status")
async def baekjoon_status(user: User = Depends(get_current_user)):
    if not user.baekjoon_handle:
        return {"linked": False}
    return {
        "linked": True,
        "handle": user.baekjoon_handle,
        "tier": user.baekjoon_tier,
        "tier_name": tier_name(user.baekjoon_tier),
    }


@router.post("/baekjoon/refresh")
async def refresh_baekjoon_tier(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not user.baekjoon_handle:
        raise HTTPException(status_code=400, detail="백준 연동이 되어있지 않습니다.")
    solved_user = await _fetch_solved_ac_user(user.baekjoon_handle)
    if not solved_user:
        raise HTTPException(status_code=502, detail="solved.ac에서 정보를 조회할 수 없습니다.")
    tier = solved_user.get("tier", 0)
    user.baekjoon_tier = tier
    await db.commit()
    return {"handle": user.baekjoon_handle, "tier": tier, "tier_name": tier_name(tier)}
