"""보안 헤더 미들웨어 — OWASP 권장 응답 헤더 자동 추가 (순수 ASGI)."""

from starlette.types import ASGIApp, Receive, Scope, Send


class SecurityHeadersMiddleware:
    """모든 HTTP 응답에 보안 헤더를 추가한다 (순수 ASGI 구현)."""

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
        self._managed_names = frozenset(name for name, _ in self._headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [
                    (k, v) for k, v in message.get("headers", [])
                    if k.lower() != b"server" and k.lower() not in self._managed_names
                ]
                headers.extend(self._headers)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
