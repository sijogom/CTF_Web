"""Static contract checks for the WSL development portproxy helper."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DeployWslPortproxyContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = (PROJECT_ROOT / "infra" / "scripts" / "deploy-wsl.sh").read_text(
            encoding="utf-8"
        )

    def test_portproxy_preserves_unrelated_windows_rules(self) -> None:
        self.assertNotIn("netsh interface portproxy reset", self.script)
        self.assertIn("기존 Windows 80 portproxy 규칙을 덮어쓰지 않습니다.", self.script)

    def test_development_caddy_listener_contract_is_loopback_80_to_wsl_8080(self) -> None:
        expected_command = (
            "netsh interface portproxy add v4tov4 "
            "listenaddress=127.0.0.1 listenport=80 "
            "connectaddress=$wsl_ip connectport=8080"
        )
        self.assertIn(expected_command, self.script)
        self.assertIn('expected_rule="127.0.0.1.*80.*${wsl_ip}.*8080"', self.script)
        self.assertNotIn("listenport=443", self.script)
        self.assertIn("http://localhost (Windows loopback portproxy 사용 시)", self.script)
        self.assertNotIn('echo -e "  ${BOLD}접속 URL:${NC}       https://$domain"', self.script)


if __name__ == "__main__":
    unittest.main()
