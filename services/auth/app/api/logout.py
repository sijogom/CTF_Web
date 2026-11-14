import logging

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.jwt.token import decode_token
from app.redis import redis_client
from app.utils.cookies import delete_auth_cookies
from app.utils.audit import record_audit

router = APIRouter(tags=["인증"])
logger = logging.getLogger(__name__)


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(
    request: Request,
    response: Response,
    refresh_token: str = Cookie(None),
    db: AsyncSession = Depends(get_db),
):
    """로그아웃 — Redis에서 refresh token jti 삭제 + 쿠키 제거"""
    actor_id: str | None = None
    refresh_token_revoked = False
    if refresh_token:
        payload = decode_token(refresh_token)
        if payload and payload.get("type") == "refresh":
            jti = payload.get("jti")
            if jti:
                await redis_client.delete(f"refresh_token:{jti}")
                refresh_token_revoked = True
                # user_tokens Set에서도 제거
                user_id = payload.get("sub")
                if user_id:
                    actor_id = str(user_id)
                    await redis_client.srem(f"user_tokens:{user_id}", jti)

    # 쿠키 삭제 (set_cookie과 동일한 속성 지정 — 불일치 시 브라우저가 삭제 거부)
    delete_auth_cookies(response)

    # 로그아웃은 인증 쿠키가 이미 제거되는 흐름이므로 별도 current-user
    # 의존성을 추가하지 않고 refresh token의 검증된 subject만 감사 주체로
    # 사용한다. 토큰·jti 자체는 감사 정보에 남기지 않는다.
    try:
        await record_audit(
            db,
            actor_id=actor_id,
            action="auth.logout",
            target_type="session",
            details={
                "refresh_token_present": bool(refresh_token),
                "refresh_token_revoked": refresh_token_revoked,
            },
            request=request,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        # 로그아웃 자체는 이미 Redis와 쿠키에서 처리되었으므로 감사 저장
        # 장애가 세션 종료를 되돌리거나 사용자에게 500을 만들지 않게 한다.
        logger.warning("로그아웃 감사 기록 저장 실패", exc_info=True)

    return {"message": "로그아웃 되었습니다."}
