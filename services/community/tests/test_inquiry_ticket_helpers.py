"""Database-free contract tests for inquiry guest-access helpers."""

from __future__ import annotations

import hashlib
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


def _load_helpers():
    source_path = Path(__file__).resolve().parents[1] / "app" / "utils" / "inquiry_ticket.py"
    spec = importlib.util.spec_from_file_location("inquiry_ticket_helpers", source_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InquiryTicketHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.helpers = _load_helpers()
        cls.challenge_id = uuid4()
        cls.inquiry_id = uuid4()
        cls.expires_at = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)

    def test_token_digest_is_deterministic_sha256_without_the_raw_token(self) -> None:
        raw_token = "opaque-ticket-session"
        digest = self.helpers.token_digest(raw_token)

        self.assertEqual(digest, hashlib.sha256(raw_token.encode("utf-8")).hexdigest())
        self.assertNotEqual(digest, raw_token)
        self.assertNotEqual(digest, self.helpers.token_digest("different-token"))

    def test_proof_is_bound_to_challenge_expiry_and_secret(self) -> None:
        proof = self.helpers.sign_access_proof(
            self.challenge_id,
            self.expires_at,
            "test-secret",
        )

        self.assertTrue(
            self.helpers.verify_access_proof(
                self.challenge_id,
                self.expires_at,
                proof,
                "test-secret",
                now=self.expires_at - timedelta(seconds=1),
            )
        )
        self.assertFalse(
            self.helpers.verify_access_proof(
                self.challenge_id,
                self.expires_at,
                proof,
                "other-secret",
                now=self.expires_at - timedelta(seconds=1),
            )
        )
        self.assertFalse(
            self.helpers.verify_access_proof(
                self.challenge_id,
                self.expires_at,
                proof,
                "test-secret",
                now=self.expires_at,
            )
        )

    def test_guest_access_path_keeps_the_proof_out_of_the_query_string(self) -> None:
        path = self.helpers.build_guest_access_path(
            self.challenge_id,
            self.inquiry_id,
            self.expires_at,
            "test-secret",
        )

        self.assertTrue(path.startswith("/inquiry/access#ticket="))
        self.assertIn(f"ticket={self.inquiry_id}", path)
        self.assertIn(f"challenge={self.challenge_id}", path)
        self.assertIn("&proof=", path)
        self.assertNotIn("?proof=", path)

    def test_retry_and_error_helpers_are_bounded_and_log_safe(self) -> None:
        self.assertEqual(self.helpers.retry_delay(0), timedelta(minutes=1))
        self.assertEqual(self.helpers.retry_delay(2), timedelta(minutes=2))
        self.assertEqual(self.helpers.retry_delay(99), timedelta(hours=1))
        self.assertEqual(
            self.helpers.sanitize_delivery_error("upstream failed\r\nretry later"),
            "upstream failed retry later",
        )
        self.assertIsNone(self.helpers.sanitize_delivery_error(" \r\n "))


if __name__ == "__main__":
    unittest.main()
