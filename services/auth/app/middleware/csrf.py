"""CSRF 방지 미들웨어 (Double Submit Cookie 패턴)."""

import hmac
import os
import secrets
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

from app.config import settings
from app.utils.cookies import CSRF_COOKIE, CSRF_PATH

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# CSRF 검증을 면제할 경로 접두사/경로 목록
# - 인증 전 접근하는 경로 (CSRF 쿠키 미보유 가능)
# - 내부 서비스 간 통신 경로
# - Refresh Token은 HttpOnly 쿠키로 별도 보호
EXEMPT_PREFIXES = ("/internal/", "/health", "/metrics", "/docs", "/openapi.json")
EXEMPT_PATHS_EXACT = {
    "/login", "/register",
    "/refresh", "/logout",
    "/forgot-password", "/reset-password",
    "/send-verification", "/verify-email", "/verify-email/code",
}


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 안전한 메서드는 통과 + CSRF 토큰 쿠키 발급
        if request.method in SAFE_METHODS:
            response = await call_next(request)
            if CSRF_COOKIE not in request.cookies:
                secure = settings.is_production and os.environ.get("COOKIE_SECURE", "true").lower() != "false"
                token = secrets.token_urlsafe(32)
                response.set_cookie(
                    CSRF_COOKIE,
                    token,
                    httponly=False,  # JS에서 읽어서 헤더로 전송해야 함
                    samesite="lax",
                    secure=secure,
                    max_age=settings.access_token_expire_minutes * 60,
                    path=CSRF_PATH,
                )
            return response

        # Bearer 토큰 인증은 CSRF 면제 (CSRF는 쿠키 자동 전송 공격 방지용)
        # Authorization 헤더는 공격자가 위조할 수 없으므로 CSRF 보호 불필요
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            return await call_next(request)

        # 면제 경로 체크 (정확한 경로 매칭 + 접두사 매칭)
        path = request.url.path
        if path in EXEMPT_PATHS_EXACT:
            return await call_next(request)
        for prefix in EXEMPT_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # 상태 변경 메서드: CSRF 토큰 검증
        # 동일 도메인 내 다른 서비스(OJ 등)가 같은 이름의 쿠키를 다른 Path로
        # 설정할 수 있어 중복 쿠키가 발생한다. Cookie 헤더에서 모든 값을 추출하여
        # X-CSRF-Token 헤더와 하나라도 일치하면 통과시킨다.
        header_token = request.headers.get("x-csrf-token")
        if not header_token:
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF 검증 실패"},
            )

        cookie_header = request.headers.get("cookie", "")
        cookie_tokens = [
            part.split("=", 1)[1].strip()
            for part in cookie_header.split(";")
            if part.strip().startswith(f"{CSRF_COOKIE}=")
        ]

        if not cookie_tokens or not any(
            hmac.compare_digest(ct, header_token) for ct in cookie_tokens
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF 검증 실패"},
            )

        return await call_next(request)
