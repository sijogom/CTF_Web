import unittest
from datetime import timedelta
from types import SimpleNamespace

from app.config import settings
from app.utils.email_verification import (
    clear_verification_artifacts,
    issue_verification_code,
    utc_now,
    verify_verification_code,
)


def make_user() -> SimpleNamespace:
    return SimpleNamespace(
        id="test-user-id",
        email_verified=False,
        email_verify_token="legacy-token",
        email_verify_expires=utc_now() + timedelta(hours=1),
        email_verify_code_required=False,
        email_verify_code_hash=None,
        email_verify_code_expires=None,
        email_verify_code_sent_at=None,
        email_verify_code_attempts=0,
    )


class EmailVerificationCodeTests(unittest.TestCase):
    def test_issued_code_requires_matching_user_scoped_hmac(self):
        user = make_user()
        code = issue_verification_code(user)

        self.assertTrue(user.email_verify_code_required)
        self.assertNotEqual(user.email_verify_code_hash, code)
        self.assertEqual(verify_verification_code(user, code), "success")

    def test_invalid_attempts_lock_code_after_configured_limit(self):
        user = make_user()
        code = issue_verification_code(user)
        wrong_code = "000000" if code != "000000" else "999999"

        for _ in range(settings.email_verify_code_max_attempts - 1):
            self.assertEqual(verify_verification_code(user, wrong_code), "invalid")
        self.assertEqual(verify_verification_code(user, wrong_code), "locked")
        self.assertEqual(user.email_verify_code_attempts, settings.email_verify_code_max_attempts)

    def test_success_clears_code_and_legacy_link_artifacts(self):
        user = make_user()
        code = issue_verification_code(user)
        self.assertEqual(verify_verification_code(user, code), "success")

        clear_verification_artifacts(user)

        self.assertTrue(user.email_verified)
        self.assertIsNone(user.email_verify_token)
        self.assertIsNone(user.email_verify_code_hash)
        self.assertIsNone(user.email_verify_code_expires)
        self.assertEqual(user.email_verify_code_attempts, 0)


if __name__ == "__main__":
    unittest.main()
