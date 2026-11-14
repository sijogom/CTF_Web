import unittest

import httpx
from fastapi import FastAPI

from app.middleware.csrf import CSRFMiddleware


class EmailVerificationCsrfTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthenticated_code_verification_is_exactly_exempt(self):
        app = FastAPI()
        app.add_middleware(CSRFMiddleware)

        @app.post("/verify-email/code")
        async def verify_code():
            return {"accepted": True}

        @app.post("/verify-email/code/extra")
        async def unexpected_subpath():
            return {"accepted": True}

        @app.post("/profile")
        async def update_profile():
            return {"accepted": True}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            verification = await client.post("/verify-email/code")
            extra_subpath = await client.post("/verify-email/code/extra")
            protected = await client.post("/profile")

        self.assertEqual(verification.status_code, 200)
        self.assertEqual(verification.json(), {"accepted": True})
        self.assertEqual(extra_subpath.status_code, 403)
        self.assertEqual(protected.status_code, 403)


if __name__ == "__main__":
    unittest.main()
