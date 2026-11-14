from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.jwt.token import decode_token, create_access_token, create_refresh_token
from app.models.user import User
from app.redis import redis_client
from app.schemas.auth import TokenResponse
from app.utils.cookies import set_auth_cookies
from app.utils.streak import update_streak
from app.utils.bans import banned_error_detail, get_active_ban

router = APIRouter(tags=["인증"])

# Redis 키 패턴:
#   refresh_token:{jti} → user_id      (TTL = refresh_token_expire_days)
#   user_tokens:{user_id} → Set[jti]   (사용자별 jti 목록, 일괄 폐기용)


async def _register_refresh_token(user_id: str, jti: str, ttl_seconds: int) -> None:
    """Refresh token의 jti를 Redis에 등록 (토큰 + 사용자별 Set 모두)"""
    pipe = redis_client.pipeline()
    pipe.setex(f"refresh_token:{jti}", ttl_seconds, user_id)
    pipe.sadd(f"user_tokens:{user_id}", jti)
    pipe.expire(f"user_tokens:{user_id}", ttl_seconds)
    await pipe.execute()


async def _revoke_refresh_token(user_id: str, jti: str) -> None:
    """단일 refresh token 폐기"""
    pipe = redis_client.pipeline()
    pipe.delete(f"refresh_token:{jti}")
    pipe.srem(f"user_tokens:{user_id}", jti)
    await pipe.execute()


async def _revoke_all_user_tokens(user_id: str) -> None:
    """토큰 재사용 감지 시 해당 사용자의 모든 refresh token을 폐기하는 보안 조치"""
    jtis = await redis_client.smembers(f"user_tokens:{user_id}")
    if jtis:
        pipe = redis_client.pipeline()
        for jti in jtis:
            pipe.delete(f"refresh_token:{jti}")
        pipe.delete(f"user_tokens:{user_id}")
        await pipe.execute()


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    response: Response,
    db: AsyncSession = Depends(get_db),
    refresh_token: str = Cookie(None),
):
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="리프레시 토큰이 없습니다.")

    payload = decode_token(refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 리프레시 토큰입니다.")

    jti = payload.get("jti")
    user_id = payload.get("sub")

    # jti가 Redis에 유효한지 확인 (1회용 사용 보장)
    stored_user_id = await redis_client.get(f"refresh_token:{jti}")
    if not stored_user_id or stored_user_id != user_id:
        # 토큰 재사용 감지 — Redis에 저장된 실제 소유자 기준으로 모든 토큰 폐기 (보안 조치)
        # JWT payload의 user_id가 아닌 stored_user_id를 사용해야 올바른 사용자의 토큰을 폐기한다.
        if stored_user_id:
            # stored_user_id가 user_id와 다른 경우: 토큰 변조 가능성 → 실제 소유자 보호
            await _revoke_all_user_tokens(stored_user_id)
        elif jti:
            # stored_user_id가 없는 경우(이미 소비된 토큰 재사용): JWT의 user_id로 폐기
            await _revoke_all_user_tokens(user_id)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 리프레시 토큰입니다.")

    # 이전 토큰 폐기
    await _revoke_refresh_token(user_id, jti)

    # DB에서 최신 role 조회 (role 변경 즉시 반영)
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="존재하지 않는 사용자입니다.")

    # 연속 출석 업데이트 (원자적 DB UPDATE)
    active_ban = await get_active_ban(db, user.id)
    if active_ban:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=banned_error_detail(active_ban, user.id),
        )

    await update_streak(db, user)
    await db.commit()

    # 새 토큰 발급 (Refresh Token Rotation)
    new_access = create_access_token(str(user.id), user.role, user.username)
    new_refresh = create_refresh_token(str(user.id))

    # 새 refresh token의 jti를 Redis에 등록
    new_payload = decode_token(new_refresh)
    if not new_payload:
        raise HTTPException(status_code=500, detail="Refresh token 생성 오류")
    new_jti = new_payload["jti"]
    ttl_seconds = settings.refresh_token_expire_days * 86400
    await _register_refresh_token(str(user.id), new_jti, ttl_seconds)

    set_auth_cookies(response, new_access, new_refresh, ttl_seconds)

    return TokenResponse(expires_in=settings.access_token_expire_minutes * 60)
