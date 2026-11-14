"""기존에 Discord를 연결한 플랫폼 계정의 로그인 SSO API."""

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.refresh import _register_refresh_token, _revoke_refresh_token
from app.config import settings
from app.database import get_db
from app.jwt.token import create_access_token, create_refresh_token, decode_token
from app.models.user import User
from app.redis import redis_client
from app.utils.audit import record_audit
from app.utils.bans import get_active_ban
from app.utils.cookies import set_auth_cookies
from app.utils.discord_sso import (
    DEFAULT_POST_LOGIN_PATH,
    create_discord_sso_state,
    discord_oauth_configured,
    normalize_post_login_path,
)
from app.utils.streak import update_streak


logger = logging.getLogger(__name__)

router = APIRouter(tags=["인증"])

DISCORD_API = "https://discord.com/api/v10"
DISCORD_OAUTH_AUTHORIZE = "https://discord.com/oauth2/authorize"
DISCORD_OAUTH_TOKEN = f"{DISCORD_API}/oauth2/token"
DISCORD_SSO_SCOPES = "identify"
STATE_KEY_PREFIX = "oauth:discord:sso:state:"
STATE_VERIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")


@dataclass(frozen=True)
class ConsumedSSOState:
    """Redis에서 원자적으로 꺼낸 한 번만 사용할 수 있는 state."""

    redirect: str
    code_verifier: str


def _discord_configured() -> bool:
    return discord_oauth_configured(
        settings.discord_client_id,
        settings.discord_client_secret,
    )


def _callback_url() -> str:
    return f"{settings.frontend_url.rstrip('/')}/api/auth/discord/sso/callback"


def _frontend_url(path: str) -> str:
    return f"{settings.frontend_url.rstrip('/')}{path}"


def _failed_login_redirect(outcome: str, *, locale: str = "ko") -> RedirectResponse:
    safe_locale = locale if locale in {"ko", "en", "ja", "zh"} else "ko"
    query = urlencode({"sso": outcome})
    return RedirectResponse(
        url=f"{_frontend_url(f'/{safe_locale}/login')}?{query}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _state_key(state: str) -> str:
    return f"{STATE_KEY_PREFIX}{state}"


async def _store_state(
    *,
    state: str,
    redirect: str,
    code_verifier: str,
    locale: str,
) -> None:
    """state와 PKCE verifier를 callback 전에 서버 측 Redis에만 보관한다."""
    payload = json.dumps(
        {"redirect": redirect, "code_verifier": code_verifier, "locale": locale},
        separators=(",", ":"),
    )
    await redis_client.setex(
        _state_key(state),
        settings.oauth_state_expire_minutes * 60,
        payload,
    )


async def _consume_state(state: str) -> tuple[ConsumedSSOState, str] | None:
    """유효한 state를 한 번만 반환한다. 오류·변조 state는 fail-closed 한다."""
    raw = await redis_client.getdel(_state_key(state))
    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None

    redirect = payload.get("redirect") if isinstance(payload, dict) else None
    code_verifier = payload.get("code_verifier") if isinstance(payload, dict) else None
    locale = payload.get("locale") if isinstance(payload, dict) else None
    if not isinstance(redirect, str) or not isinstance(code_verifier, str):
        return None
    if redirect != normalize_post_login_path(redirect):
        return None
    if not STATE_VERIFIER_PATTERN.fullmatch(code_verifier):
        return None
    if locale not in {"ko", "en", "ja", "zh"}:
        return None
    return ConsumedSSOState(redirect=redirect, code_verifier=code_verifier), locale


async def _audit_sso_failure(
    db: AsyncSession,
    request: Request,
    *,
    reason: str,
    actor_id: str | None = None,
) -> None:
    try:
        await record_audit(
            db,
            actor_id=actor_id,
            action="auth.sso_discord_fail",
            details={"provider": "discord", "reason": reason},
            request=request,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("Discord SSO 실패 감사 기록 저장 실패")


@router.get("/discord/sso/status")
async def discord_sso_status():
    """로그인 화면이 공개 가능한 SSO 지원 상태만 확인한다."""
    return {
        "configured": _discord_configured(),
        "prelinked_only": True,
    }


@router.get("/discord/sso/start")
async def discord_sso_start(
    redirect: str | None = Query(default=None),
    locale: str = Query(default="ko", pattern="^(ko|en|ja|zh)$"),
):
    """Discord OAuth 로그인 시작 URL을 만든다."""
    if not _discord_configured():
        return {"configured": False, "url": None}

    destination = normalize_post_login_path(redirect)
    state = create_discord_sso_state()
    try:
        await _store_state(
            state=state.state,
            redirect=destination,
            code_verifier=state.code_verifier,
            locale=locale,
        )
    except Exception:
        logger.exception("Discord SSO state 저장 실패")
        # state를 저장하지 못하면 callback을 안전하게 검증할 수 없다.
        return {"configured": False, "url": None}

    params = {
        "client_id": settings.discord_client_id,
        "redirect_uri": _callback_url(),
        "response_type": "code",
        "scope": DISCORD_SSO_SCOPES,
        "state": state.state,
        "code_challenge": state.code_challenge,
        "code_challenge_method": "S256",
    }
    return {
        "configured": True,
        "url": f"{DISCORD_OAUTH_AUTHORIZE}?{urlencode(params)}",
    }


@router.get("/discord/sso/callback")
async def discord_sso_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    code: str | None = Query(default=None, max_length=4096),
    state: str | None = Query(default=None, max_length=512),
    error: str | None = Query(default=None, max_length=100),
):
    """Discord code를 사전 연결된 플랫폼 계정 세션으로 교환한다."""
    if not state or not _discord_configured():
        return _failed_login_redirect("failed")
    if not STATE_VERIFIER_PATTERN.fullmatch(state):
        return _failed_login_redirect("failed")

    locale = "ko"
    try:
        consumed = await _consume_state(state)
    except Exception:
        logger.exception("Discord SSO state 소비 실패")
        return _failed_login_redirect("unavailable", locale=locale)
    if not consumed:
        return _failed_login_redirect("failed", locale=locale)
    consumed_state, locale = consumed
    if error or not code:
        await _audit_sso_failure(db, request, reason="provider_cancelled_or_invalid_callback")
        return _failed_login_redirect("failed", locale=locale)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_response = await client.post(
                DISCORD_OAUTH_TOKEN,
                data={
                    "client_id": settings.discord_client_id,
                    "client_secret": settings.discord_client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _callback_url(),
                    "code_verifier": consumed_state.code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError:
        logger.warning("Discord SSO token 교환 네트워크 실패")
        await _audit_sso_failure(db, request, reason="token_exchange_network_failed")
        return _failed_login_redirect("unavailable", locale=locale)

    if token_response.status_code != status.HTTP_200_OK:
        logger.warning("Discord SSO token 교환 거부: status=%s", token_response.status_code)
        await _audit_sso_failure(db, request, reason="token_exchange_failed")
        return _failed_login_redirect("failed", locale=locale)

    try:
        token_payload = token_response.json()
    except ValueError:
        await _audit_sso_failure(db, request, reason="token_exchange_invalid_response")
        return _failed_login_redirect("failed", locale=locale)

    access_token = token_payload.get("access_token") if isinstance(token_payload, dict) else None
    if not isinstance(access_token, str) or not access_token:
        await _audit_sso_failure(db, request, reason="token_exchange_invalid_response")
        return _failed_login_redirect("failed", locale=locale)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            user_response = await client.get(
                f"{DISCORD_API}/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError:
        logger.warning("Discord SSO identify 조회 네트워크 실패")
        await _audit_sso_failure(db, request, reason="identify_network_failed")
        return _failed_login_redirect("unavailable", locale=locale)

    if user_response.status_code != status.HTTP_200_OK:
        logger.warning("Discord SSO identify 조회 거부: status=%s", user_response.status_code)
        await _audit_sso_failure(db, request, reason="identify_failed")
        return _failed_login_redirect("failed", locale=locale)

    try:
        provider_payload = user_response.json()
    except ValueError:
        await _audit_sso_failure(db, request, reason="identify_invalid_response")
        return _failed_login_redirect("failed", locale=locale)

    provider_id = provider_payload.get("id") if isinstance(provider_payload, dict) else None
    if (
        not isinstance(provider_id, str)
        or not provider_id.isascii()
        or not provider_id.isdecimal()
        or len(provider_id) > 50
    ):
        await _audit_sso_failure(db, request, reason="identify_invalid_response")
        return _failed_login_redirect("failed", locale=locale)

    result = await db.execute(
        select(User).where(User.discord_id == provider_id).with_for_update()
    )
    user = result.scalar_one_or_none()
    if not user:
        await _audit_sso_failure(db, request, reason="unlinked_discord_account")
        return _failed_login_redirect("denied", locale=locale)
    if not user.is_active:
        await _audit_sso_failure(
            db,
            request,
            actor_id=str(user.id),
            reason="inactive_account",
        )
        return _failed_login_redirect("denied", locale=locale)
    if not user.email_verified:
        await _audit_sso_failure(
            db,
            request,
            actor_id=str(user.id),
            reason="email_not_verified",
        )
        return _failed_login_redirect("denied", locale=locale)

    active_ban = await get_active_ban(db, user.id, for_update=True)
    if active_ban:
        await _audit_sso_failure(
            db,
            request,
            actor_id=str(user.id),
            reason="active_ban",
        )
        return _failed_login_redirect("denied", locale=locale)

    await update_streak(db, user)
    access = create_access_token(str(user.id), user.role, user.username)
    refresh = create_refresh_token(str(user.id))
    refresh_payload = decode_token(refresh)
    if not refresh_payload or not isinstance(refresh_payload.get("jti"), str):
        logger.error("Discord SSO refresh token 생성 실패")
        await _audit_sso_failure(
            db,
            request,
            actor_id=str(user.id),
            reason="session_issue_failed",
        )
        return _failed_login_redirect("unavailable", locale=locale)

    refresh_ttl_seconds = settings.refresh_token_expire_days * 86400
    try:
        await _register_refresh_token(
            str(user.id),
            refresh_payload["jti"],
            refresh_ttl_seconds,
        )
    except Exception:
        logger.exception("Discord SSO refresh token 등록 실패")
        await _audit_sso_failure(
            db,
            request,
            actor_id=str(user.id),
            reason="session_store_failed",
        )
        return _failed_login_redirect("unavailable", locale=locale)

    try:
        await record_audit(
            db,
            actor_id=str(user.id),
            action="auth.sso_discord_login",
            details={"provider": "discord", "prelinked": True},
            request=request,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        try:
            await _revoke_refresh_token(str(user.id), refresh_payload["jti"])
        except Exception:
            logger.exception("Discord SSO 실패 후 refresh token 정리 실패")
        logger.exception("Discord SSO 로그인 기록 저장 실패")
        return _failed_login_redirect("unavailable", locale=locale)

    response = RedirectResponse(
        url=_frontend_url(consumed_state.redirect or DEFAULT_POST_LOGIN_PATH),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    set_auth_cookies(response, access, refresh, refresh_ttl_seconds)
    return response
