"""보안 헤더 미들웨어 — OWASP 권장 응답 헤더 자동 추가 (순수 ASGI)."""

from starlette.types import ASGIApp, Receive, Scope, Send


class SecurityHeadersMiddleware:
    """모든 HTTP 응답에 보안 헤더를 추가한다 (순수 ASGI 구현).

    - X-Content-Type-Options: MIME sniffing 방지
    - X-Frame-Options: Clickjacking 방지
    - Referrer-Policy: 외부 사이트로 경로 노출 제한
    - Permissions-Policy: 브라우저 API 접근 제한
    - X-XSS-Protection: 구형 브라우저 XSS 필터 활성화
    - Strict-Transport-Security: HTTPS 강제 (프로덕션)
    - Cache-Control: API 응답 캐시 방지
    - Server 헤더 제거: 서버 소프트웨어 정보 노출 방지
    """

    def __init__(self, app: ASGIApp, is_production: bool = False):
        self.app = app
        self.is_production = is_production
        self._headers: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
            (b"x-xss-protection", b"1; mode=block"),
            (b"cache-control", b"no-store, no-cache, must-revalidate"),
            (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
        ]
        if is_production:
            self._headers.append(
                (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [
                    (k, v) for k, v in message.get("headers", [])
                    if k.lower() != b"server"
                ]
                headers.extend(self._headers)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
