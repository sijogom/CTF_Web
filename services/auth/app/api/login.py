from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, TokenResponse
from app.jwt.token import create_access_token, create_refresh_token, decode_token
from app.config import settings
from app.security import pwd_context  # 공유 bcrypt 인스턴스 (rounds=12)
from app.api.refresh import _register_refresh_token
from app.utils.audit import record_audit
from app.utils.cookies import set_auth_cookies
from app.utils.streak import update_streak
from app.utils.bans import banned_error_detail, get_active_ban

router = APIRouter(tags=["인증"])


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    # 유저 조회
    result = await db.execute(select(User).where(User.email == req.email))
    user = result.scalar_one_or_none()

    if not user or not user.password_hash:
        await record_audit(
            db, actor_id=None, action="auth.login_fail",
            details={"email": req.email, "reason": "user_not_found"}, request=request,
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="이메일 또는 비밀번호가 올바르지 않습니다.")

    if not pwd_context.verify(req.password, user.password_hash):
        await record_audit(
            db, actor_id=str(user.id), action="auth.login_fail",
            details={"email": req.email, "reason": "wrong_password"}, request=request,
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="이메일 또는 비밀번호가 올바르지 않습니다.")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="비활성화된 계정입니다.")

    active_ban = await get_active_ban(db, user.id)
    if active_ban:
        await record_audit(
            db,
            actor_id=str(user.id),
            action="auth.login_banned",
            target_type="ban",
            target_id=str(active_ban.id),
            details={"email": req.email},
            request=request,
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=banned_error_detail(active_ban, user.id),
        )

    if not user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="EMAIL_NOT_VERIFIED",
        )

    # 연속 출석 업데이트 (원자적 DB UPDATE)
    await update_streak(db, user)

    # 토큰 발급
    access_token = create_access_token(str(user.id), user.role, user.username)
    refresh_token = create_refresh_token(str(user.id))

    # Refresh Token의 jti를 Redis에 등록 (공통 헬퍼 사용)
    rt_payload = decode_token(refresh_token)
    if not rt_payload:
        raise HTTPException(status_code=500, detail="Refresh token 생성 오류")
    jti = rt_payload["jti"]
    ttl_seconds = settings.refresh_token_expire_days * 86400
    await _register_refresh_token(str(user.id), jti, ttl_seconds)

    set_auth_cookies(response, access_token, refresh_token, ttl_seconds)

    # 로그인 성공 감사 로그
    await record_audit(
        db, actor_id=str(user.id), action="auth.login",
        details={"email": req.email}, request=request,
    )
    await db.commit()

    return TokenResponse(
        expires_in=settings.access_token_expire_minutes * 60,
    )
