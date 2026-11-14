"""인증 쿠키 설정/삭제 헬퍼.

login, refresh, logout 등에서 반복되는 쿠키 설정을 한 곳에서 관리한다.
"""

import secrets

from fastapi import Response

from app.config import settings

# ── 쿠키 이름 상수 ──────────────────────────────
ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"
CSRF_COOKIE = "ctf_csrf_token"

# ── 경로 상수 ───────────────────────────────────
# 서브패스 배포 시 COOKIE_BASE_PATH 환경변수로 조정 (예: /ctf)
import os
_BASE = os.environ.get("COOKIE_BASE_PATH", "")
ACCESS_PATH = f"{_BASE}/"
REFRESH_PATH = f"{_BASE}/api/auth/"
CSRF_PATH = f"{_BASE}/"


def set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
    refresh_ttl_seconds: int,
) -> None:
    """Access/Refresh/CSRF 쿠키를 한 번에 설정한다."""
    secure = settings.is_production and os.environ.get("COOKIE_SECURE", "true").lower() != "false"
    access_max_age = settings.access_token_expire_minutes * 60

    # Access Token — httpOnly (XSS 토큰 탈취 방지)
    response.set_cookie(
        key=ACCESS_COOKIE,
        value=access_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=access_max_age,
        path=ACCESS_PATH,
    )

    # Refresh Token — httpOnly (경로 제한)
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=refresh_token,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=refresh_ttl_seconds,
        path=REFRESH_PATH,
    )

    # CSRF Token — JS에서 읽어서 X-CSRF-Token 헤더로 전송
    csrf_token = secrets.token_urlsafe(32)
    response.set_cookie(
        key=CSRF_COOKIE,
        value=csrf_token,
        httponly=False,
        secure=secure,
        samesite="lax",
        max_age=access_max_age,
        path=CSRF_PATH,
    )


def delete_auth_cookies(response: Response) -> None:
    """Access/Refresh/CSRF 쿠키를 모두 삭제한다."""
    secure = settings.is_production and os.environ.get("COOKIE_SECURE", "true").lower() != "false"

    response.delete_cookie(
        key=ACCESS_COOKIE, path=ACCESS_PATH,
        secure=secure, samesite="lax",
    )
    response.delete_cookie(
        key=REFRESH_COOKIE, path=REFRESH_PATH,
        secure=secure, samesite="strict",
    )
    response.delete_cookie(
        key=CSRF_COOKIE, path=CSRF_PATH,
        secure=secure, samesite="strict",
    )
