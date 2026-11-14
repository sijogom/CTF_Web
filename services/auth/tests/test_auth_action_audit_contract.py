"""인증 보조 흐름의 감사 이벤트 계약을 DB 없이 고정한다.

이 테스트는 비밀번호·로그아웃·Discord 연결 코드가 비밀값을 감사 details에
넣지 않고 성공·실패 이벤트를 기록하도록 소스 계약만 확인한다. 실제 DB와
Redis의 commit/flush 동작은 Auth 통합 테스트에서 별도로 검증해야 한다.
"""

from pathlib import Path
import unittest


SERVICE_ROOT = Path(__file__).resolve().parents[1]


class AuthActionAuditContractTests(unittest.TestCase):
    def _source(self, relative: str) -> str:
        return (SERVICE_ROOT / relative).read_text(encoding="utf-8")

    def test_logout_records_session_outcome_without_token_material(self) -> None:
        source = self._source("app/api/logout.py")
        self.assertIn('action="auth.logout"', source)
        self.assertIn('"refresh_token_present": bool(refresh_token)', source)
        self.assertIn('"refresh_token_revoked": refresh_token_revoked', source)
        self.assertNotIn('details={"jti":', source)
        self.assertNotIn('details={"refresh_token":', source)

    def test_password_flows_have_success_and_failure_actions(self) -> None:
        source = self._source("app/api/password.py")
        for action in (
            "auth.password_reset_requested",
            "auth.password_reset_failed",
            "auth.password_reset_completed",
            "auth.password_change_failed",
            "auth.password_change_completed",
        ):
            self.assertIn(f'action="{action}"', source)
        self.assertIn("토큰·비밀번호·이메일은 저장하지 않는다", source)
        self.assertNotIn('details={"token":', source)
        self.assertNotIn('details={"password":', source)

    def test_discord_link_and_unlink_are_audited_without_provider_identifier(self) -> None:
        source = self._source("app/api/discord.py")
        self.assertIn('action="auth.discord_link"', source)
        self.assertIn('action="auth.discord_unlink"', source)
        self.assertIn('"guild_role_assigned": guild_role_assigned', source)
        self.assertIn('details={"provider": "discord"}', source)
        self.assertNotIn('details={"discord_id":', source)


if __name__ == "__main__":
    unittest.main()
