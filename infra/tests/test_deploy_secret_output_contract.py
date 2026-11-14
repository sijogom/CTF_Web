"""배포 스크립트가 초기 관리자 비밀번호를 출력하지 않는지 고정한다."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DeploySecretOutputContractTests(unittest.TestCase):
    def test_admin_password_is_required_and_never_logged(self) -> None:
        for name in ("deploy.sh", "deploy-wsl.sh"):
            text = (PROJECT_ROOT / "infra" / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn("관리자 비밀번호 (8자 이상, 필수)", text, name)
            self.assertIn("관리자 비밀번호는 8자 이상 직접 입력해야 합니다", text, name)
            self.assertIn("exit 1", text, name)
            self.assertNotIn("자동 생성된 관리자 비밀번호:", text, name)
            self.assertNotIn('log_info "자동 생성: $admin_password"', text, name)
            self.assertNotRegex(text, r"(?:echo|log_(?:info|ok|warn|error))[^\n]*admin_password", name)


if __name__ == "__main__":
    unittest.main()
