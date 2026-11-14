"""Discord OAuth2 연동 API — 계정 연결/해제 + 동아리 역할 부여."""

import hashlib
import hmac
import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.api.deps import get_current_user
from app.utils.discord_sso import discord_oauth_configured
from app.utils.audit import record_audit

logger = logging.getLogger(__name__)

router = APIRouter(tags=["디스코드"])

DISCORD_API = "https://discord.com/api/v10"
DISCORD_OAUTH_AUTHORIZE = "https://discord.com/oauth2/authorize"
DISCORD_OAUTH_TOKEN = "https://discord.com/api/v10/oauth2/token"

# OAuth2 스코프: identify(사용자 정보), guilds.join(서버 초대, 선택)
DISCORD_SCOPES = "identify"


def _discord_configured() -> bool:
    """Discord OAuth 설정이 완료되었는지 확인."""
    return discord_oauth_configured(
        settings.discord_client_id,
        settings.discord_client_secret,
    )


# ──── OAuth state CSRF 보호 ────

STATE_TTL_SECONDS = settings.oauth_state_expire_minutes * 60


def _sign_state(user_id: str) -> str:
    """user_id + 타임스탬프 + nonce를 HMAC 서명하여 state 토큰 생성."""
    nonce = secrets.token_hex(8)
    ts = str(int(time.time()))
    payload = f"{user_id}:{ts}:{nonce}"
    sig = hmac.new(
        settings.jwt_secret_key.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f"{payload}:{sig}"


def _verify_state(state: str) -> str | None:
    """서명된 state를 검증하고 user_id를 반환. 실패 시 None."""
    parts = state.split(":")
    if len(parts) != 4:
        return None
    user_id, ts, nonce, sig = parts

    # 타임스탬프 유효성 검사
    try:
        if abs(time.time() - int(ts)) > STATE_TTL_SECONDS:
            return None
    except ValueError:
        return None

    # 서명 검증
    payload = f"{user_id}:{ts}:{nonce}"
    expected = hmac.new(
        settings.jwt_secret_key.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None

    return user_id


@router.get("/discord/status")
async def discord_status():
    """Discord 연동 설정 상태 확인."""
    return {
        "configured": _discord_configured(),
        "bot_configured": bool(settings.discord_bot_token and settings.discord_guild_id),
    }


@router.get("/discord/link")
async def discord_link_start(
    user: User = Depends(get_current_user),
):
    """Discord OAuth2 인증 시작 — 프론트엔드에서 이 URL로 리다이렉트."""
    if not _discord_configured():
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Discord 연동이 설정되지 않았습니다. 관리자에게 문의하세요.",
        )

    params = {
        "client_id": settings.discord_client_id,
        "redirect_uri": f"{settings.frontend_url}/api/auth/discord/callback",
        "response_type": "code",
        "scope": DISCORD_SCOPES,
        "state": _sign_state(str(user.id)),
    }
    return {"url": f"{DISCORD_OAUTH_AUTHORIZE}?{urlencode(params)}"}


@router.get("/discord/callback")
async def discord_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Discord OAuth2 콜백 — 코드 교환 후 계정 연결."""
    if not _discord_configured():
        raise HTTPException(status_code=501, detail="Discord 연동이 설정되지 않았습니다.")

    # state 서명 검증 (CSRF 방지)
    user_id = _verify_state(state)
    if not user_id:
        raise HTTPException(status_code=400, detail="유효하지 않거나 만료된 인증 요청입니다.")

    # 1. 코드 → 토큰 교환
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(
            DISCORD_OAUTH_TOKEN,
            data={
                "client_id": settings.discord_client_id,
                "client_secret": settings.discord_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{settings.frontend_url}/api/auth/discord/callback",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if token_resp.status_code != 200:
        logger.error("Discord 토큰 교환 실패: %s", token_resp.text)
        raise HTTPException(status_code=502, detail="Discord 인증에 실패했습니다.")

    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail="Discord 토큰을 받지 못했습니다.")

    # 2. Discord 사용자 정보 조회
    async with httpx.AsyncClient(timeout=10.0) as client:
        user_resp = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if user_resp.status_code != 200:
        logger.error("Discord 사용자 정보 조회 실패: %s", user_resp.text)
        raise HTTPException(status_code=502, detail="Discord 사용자 정보를 가져올 수 없습니다.")

    discord_user = user_resp.json()
    discord_id = discord_user["id"]
    discord_username = discord_user.get("global_name") or discord_user.get("username", "")

    # 3. 이미 다른 계정에 연결된 Discord ID인지 확인
    existing = await db.execute(
        select(User).where(User.discord_id == discord_id, User.id != user_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이 Discord 계정은 이미 다른 사용자에게 연결되어 있습니다.",
        )

    # 4. 사용자 찾기
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")

    # 5. Discord 정보 저장
    user.discord_id = discord_id
    user.discord_username = discord_username
    await db.commit()

    logger.info("Discord 계정 연결: user=%s, discord=%s (%s)", user.username, discord_username, discord_id)

    # 6. Discord 서버 역할 부여 (봇 토큰이 있을 때)
    guild_role_assigned = await _assign_guild_role(discord_id)
    try:
        await record_audit(
            db,
            actor_id=str(user.id),
            action="auth.discord_link",
            target_type="user",
            target_id=str(user.id),
            details={
                "provider": "discord",
                "guild_role_assigned": guild_role_assigned,
            },
            request=request,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("Discord 연결 감사 기록 저장 실패", exc_info=True)

    # 7. 프론트엔드로 리다이렉트
    from fastapi.responses import RedirectResponse
    return RedirectResponse(
        url=f"{settings.frontend_url}/settings/account?discord=linked",
        status_code=302,
    )


@router.post("/discord/unlink")
async def discord_unlink(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Discord 계정 연결 해제."""
    if not user.discord_id:
        raise HTTPException(status_code=400, detail="연결된 Discord 계정이 없습니다.")

    old_discord = user.discord_username
    user.discord_id = None
    user.discord_username = None
    await db.commit()

    try:
        await record_audit(
            db,
            actor_id=str(user.id),
            action="auth.discord_unlink",
            target_type="user",
            target_id=str(user.id),
            details={"provider": "discord"},
            request=request,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("Discord 연결 해제 감사 기록 저장 실패", exc_info=True)

    logger.info("Discord 계정 연결 해제: user=%s, was=%s", user.username, old_discord)
    return {"message": "Discord 연결이 해제되었습니다."}


async def _assign_guild_role(discord_id: str) -> bool:
    """Discord 봇으로 서버 멤버에게 동아리 역할을 부여한다."""
    if not (settings.discord_bot_token and settings.discord_guild_id and settings.discord_member_role_id):
        logger.debug("Discord 봇 설정 미완료 — 역할 부여 스킵")
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.put(
                f"{DISCORD_API}/guilds/{settings.discord_guild_id}/members/{discord_id}/roles/{settings.discord_member_role_id}",
                headers={"Authorization": f"Bot {settings.discord_bot_token}"},
            )
        if resp.status_code in (204, 200):
            logger.info("Discord 역할 부여 성공: discord_id=%s", discord_id)
            return True
        else:
            logger.warning("Discord 역할 부여 실패: %s %s", resp.status_code, resp.text)
            return False
    except Exception as exc:
        logger.error("Discord 역할 부여 중 오류: %s", exc)
        return False
