import unittest

from pydantic import ValidationError

from app.schemas.auth import ALLOWED_EMAIL_DOMAINS, RegisterRequest


VALID_PASSWORD = "StrongPass1!"


class RegistrationDomainPolicyTests(unittest.TestCase):
    def make_request(self, email: str) -> RegisterRequest:
        return RegisterRequest(
            email=email,
            username="registration_test_user",
            password=VALID_PASSWORD,
        )

    def test_only_cju_ac_kr_is_configured_for_registration(self):
        self.assertEqual(ALLOWED_EMAIL_DOMAINS, ["cju.ac.kr"])

    def test_cju_ac_kr_registration_email_is_accepted_case_insensitively(self):
        request = self.make_request("student@CJU.AC.KR")

        self.assertEqual(request.email.split("@", 1)[1].lower(), "cju.ac.kr")

    def test_external_and_subdomain_registration_emails_are_rejected(self):
        rejected_emails = (
            "student@external.example",
            "student@another-external.example",
            "student@sub.cju.ac.kr",
            "student@cju.ac.kr.evil.example",
        )

        for email in rejected_emails:
            with self.subTest(email=email):
                with self.assertRaises(ValidationError):
                    self.make_request(email)


if __name__ == "__main__":
    unittest.main()
