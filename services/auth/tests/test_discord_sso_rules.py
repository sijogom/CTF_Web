import base64
import hashlib
from pathlib import Path
import unittest

from app.utils.discord_sso import (
    DEFAULT_POST_LOGIN_PATH,
    create_discord_sso_state,
    discord_oauth_configured,
    normalize_post_login_path,
)


class DiscordSSORulesTests(unittest.TestCase):
    def test_internal_post_login_path_is_preserved(self):
        self.assertEqual(
            normalize_post_login_path("/ko/wargame?challenge=42"),
            "/ko/wargame?challenge=42",
        )

    def test_unsafe_post_login_path_uses_default(self):
        unsafe_values = (
            "https://attacker.example/path",
            "//attacker.example/path",
            "/\\attacker.example/path",
            "/%2F%2Fattacker.example/path",
            "javascript:alert(1)",
            "/ko/wargame\nSet-Cookie: bad",
            "relative/path",
            None,
        )

        for value in unsafe_values:
            with self.subTest(value=value):
                self.assertEqual(normalize_post_login_path(value), DEFAULT_POST_LOGIN_PATH)

    def test_sso_state_uses_pkce_s256_and_sufficient_entropy(self):
        state = create_discord_sso_state()

        self.assertGreaterEqual(len(state.state), 43)
        self.assertGreaterEqual(len(state.code_verifier), 43)
        expected_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(state.code_verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        self.assertEqual(state.code_challenge, expected_challenge)

    def test_oauth_requires_both_client_values(self):
        self.assertTrue(discord_oauth_configured("client", "secret"))
        self.assertFalse(discord_oauth_configured("client", ""))
        self.assertFalse(discord_oauth_configured("", "secret"))


class DiscordSSOEndpointContractTests(unittest.TestCase):
    def test_endpoint_keeps_state_pkce_and_prelinked_account_guards(self):
        source_path = Path(__file__).resolve().parents[1] / "app/api/discord_sso.py"
        with source_path.open(encoding="utf-8") as source_file:
            source = source_file.read()

        for required_fragment in (
            "redis_client.getdel",
            '"code_challenge_method": "S256"',
            '"code_verifier": consumed_state.code_verifier',
            "select(User).where(User.discord_id == provider_id).with_for_update()",
            "if not user.email_verified:",
            "get_active_ban(db, user.id, for_update=True)",
            "set_auth_cookies(response, access, refresh, refresh_ttl_seconds)",
            'action="auth.sso_discord_login"',
        ):
            with self.subTest(required_fragment=required_fragment):
                self.assertIn(required_fragment, source)

        self.assertNotIn("User(email=", source)
        self.assertNotIn("User(username=", source)


if __name__ == "__main__":
    unittest.main()
