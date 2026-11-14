import unittest

from app.middleware.security_headers import SecurityHeadersMiddleware


class SecurityHeadersTests(unittest.IsolatedAsyncioTestCase):
    async def response(self, headers, *, production=False):
        sent = []
        body = [
            {"type": "http.response.body", "body": b"first", "more_body": True},
            {"type": "http.response.body", "body": b"last", "more_body": False},
        ]

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": headers})
            for message in body:
                await send(message)

        async def send(message):
            sent.append(message)

        await SecurityHeadersMiddleware(app, is_production=production)({"type": "http"}, None, send)
        self.assertEqual(sent[1:], body)
        return sent[0]["headers"]

    async def test_duplicate_mixed_case_managed_headers_become_one_authoritative_value(self):
        headers = await self.response([
            (b"X-Content-Type-Options", b"nosniff"), (b"x-content-type-options", b"invalid"),
            (b"X-Frame-Options", b"SAMEORIGIN"), (b"Cache-Control", b"public, max-age=3600"),
            (b"Content-Security-Policy", b"default-src *"), (b"SERVER", b"private implementation"),
        ])
        self.assertEqual([value for name, value in headers if name.lower() == b"x-content-type-options"], [b"nosniff"])
        self.assertEqual([value for name, value in headers if name.lower() == b"x-frame-options"], [b"DENY"])
        self.assertEqual([value for name, value in headers if name.lower() == b"cache-control"], [b"no-store, no-cache, must-revalidate"])
        self.assertEqual([value for name, value in headers if name.lower() == b"content-security-policy"], [b"default-src 'none'; frame-ancestors 'none'"])
        self.assertFalse(any(name.lower() == b"server" for name, _ in headers))

    async def test_unmanaged_content_and_multiple_cookies_are_preserved(self):
        original = [(b"content-type", b"application/octet-stream"), (b"content-disposition", b'attachment; filename="owned.txt"'),
                    (b"set-cookie", b"a=1; HttpOnly"), (b"set-cookie", b"b=2; HttpOnly")]
        headers = await self.response(original)
        self.assertEqual(headers[:len(original)], original)

    async def test_production_hsts_is_single_and_development_does_not_add_it(self):
        headers = await self.response([(b"Strict-Transport-Security", b"max-age=0")], production=True)
        self.assertEqual([value for name, value in headers if name.lower() == b"strict-transport-security"], [b"max-age=31536000; includeSubDomains"])
        self.assertFalse(any(name == b"strict-transport-security" for name, _ in await self.response([])))

    async def test_non_http_messages_are_not_rewritten(self):
        original = {"type": "websocket.accept", "headers": [(b"server", b"unchanged")]}
        sent = []

        async def app(scope, receive, send):
            await send(original)

        async def send(message):
            sent.append(message)

        await SecurityHeadersMiddleware(app)({"type": "websocket"}, None, send)
        self.assertEqual(sent, [original])


if __name__ == "__main__":
    unittest.main()
