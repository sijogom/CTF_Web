"""ASGI contract for the guest inquiry proof exchange response."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import inquiries
from app.models.inquiry import InquiryAccessSession, InquiryEvent
from app.utils.inquiry_ticket import sign_access_proof


class _Result:
    def __init__(self, value) -> None:  # type: ignore[no-untyped-def]
        self.value = value

    def scalar_one_or_none(self):  # type: ignore[no-untyped-def]
        return self.value


class _FakeSession:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.added: list[object] = []
        self.commit_count = 0

    async def execute(self, _statement):  # type: ignore[no-untyped-def]
        if not self.results:
            raise AssertionError("unexpected database query")
        return _Result(self.results.pop(0))

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commit_count += 1


class InquiryExchangeResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_exchange_is_empty_204_with_ticket_cookie_and_one_time_proof(self) -> None:
        inquiry_id = uuid4()
        other_inquiry_id = uuid4()
        challenge_id = uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        secret = "acceptance-inquiry-proof-secret"
        inquiry = SimpleNamespace(
            id=inquiry_id,
            user_id=None,
            subject="문의",
            content="<p>본문</p>",
            status="open",
            created_at=datetime.now(timezone.utc),
            replies=[],
        )
        other_inquiry = SimpleNamespace(
            id=other_inquiry_id,
            user_id=None,
            subject="다른 문의",
            content="<p>다른 본문</p>",
            status="open",
            created_at=datetime.now(timezone.utc),
            replies=[],
        )
        challenge = SimpleNamespace(
            id=challenge_id,
            inquiry_id=inquiry_id,
            inquiry=inquiry,
            expires_at=expires_at,
            used_at=None,
        )
        stored_session = SimpleNamespace(id=uuid4())
        fake_db = _FakeSession(
            [challenge, inquiry, stored_session, other_inquiry, challenge]
        )

        async def override_db():  # type: ignore[no-untyped-def]
            yield fake_db

        async def anonymous_user():
            return None

        app = FastAPI()
        app.include_router(inquiries.router)
        app.dependency_overrides[inquiries.get_db] = override_db
        app.dependency_overrides[inquiries.get_optional_user_id] = anonymous_user
        proof = sign_access_proof(challenge_id, expires_at, secret)

        with patch.object(inquiries.settings, "internal_api_key", secret):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                exchanged = await client.post(
                    "/inquiries/guest-access/exchange",
                    json={"challenge_id": str(challenge_id), "proof": proof},
                )
                self.assertEqual(exchanged.status_code, 204)
                self.assertEqual(exchanged.content, b"")
                cookie = exchanged.headers.get("set-cookie", "").lower()
                self.assertIn(f"inquiry_guest_session_{inquiry_id.hex}=", cookie)
                self.assertIn("httponly", cookie)
                self.assertIn("path=/", cookie)
                self.assertIn("samesite=lax", cookie)

                owner_view = await client.get(f"/inquiries/{inquiry_id}/viewer")
                self.assertEqual(owner_view.status_code, 200)
                self.assertEqual(owner_view.json()["content"], "<p>본문</p>")

                other_view = await client.get(
                    f"/inquiries/{other_inquiry_id}/viewer"
                )
                self.assertEqual(other_view.status_code, 403)

                replayed = await client.post(
                    "/inquiries/guest-access/exchange",
                    json={"challenge_id": str(challenge_id), "proof": proof},
                )
                self.assertEqual(replayed.status_code, 401)

        self.assertEqual(fake_db.commit_count, 1)
        self.assertEqual(
            sum(isinstance(item, InquiryAccessSession) for item in fake_db.added),
            1,
        )
        self.assertEqual(
            sum(isinstance(item, InquiryEvent) for item in fake_db.added),
            1,
        )


if __name__ == "__main__":
    unittest.main()
