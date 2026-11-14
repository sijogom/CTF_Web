"""Static contract for the external challenge-network lifecycle.

This suite reads source files only. It does not contact Docker, read an
environment file, create a network, or start a container.
"""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def function_body(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


class ChallengeNetworkLifecycleContractTests(unittest.TestCase):
    def setUp(self) -> None:
        infra = PROJECT_ROOT / "infra"
        self.base_compose = (infra / "docker-compose.yml").read_text(encoding="utf-8")
        self.dev_compose = (infra / "docker-compose.dev.yml").read_text(encoding="utf-8")
        self.prod_compose = (infra / "docker-compose.prod.yml").read_text(encoding="utf-8")
        self.ctfctl = (infra / "scripts" / "lib" / "ctfctl-common.sh").read_text(
            encoding="utf-8"
        )

    def test_compose_and_lifecycle_keep_the_reviewed_global_network_contract(self) -> None:
        for compose in (self.base_compose, self.dev_compose):
            self.assertIn("  ctf-challenges:\n", compose)
            self.assertIn("    name: ctf-challenges", compose)
            self.assertIn("    driver: bridge", compose)
            self.assertIn("        - subnet: 172.30.0.0/16", compose)

        prod_network = self.prod_compose.split("networks:\n", 1)[1]
        self.assertIn("  ctf-challenges:\n", prod_network)
        self.assertIn("    external: true", prod_network)
        self.assertIn("    name: ctf-challenges", prod_network)

        self.assertIn('CTFCTL_CHALLENGE_NETWORK_NAME="ctf-challenges"', self.ctfctl)
        self.assertIn('CTFCTL_CHALLENGE_NETWORK_DRIVER="bridge"', self.ctfctl)
        self.assertIn('CTFCTL_CHALLENGE_NETWORK_SUBNET="172.30.0.0/16"', self.ctfctl)

    def test_only_install_can_create_the_network_and_it_is_idempotent(self) -> None:
        helper = function_body(
            self.ctfctl,
            "ctfctl_ensure_challenge_network() {",
            "ctfctl_compose_service_running() {",
        )
        install = function_body(
            self.ctfctl,
            "ctfctl_install() {",
            "ctfctl_init_jwt_secrets() {",
        )

        self.assertIn("ctfctl_docker_is_running", helper)
        self.assertIn('docker network inspect "$CTFCTL_CHALLENGE_NETWORK_NAME"', helper)
        self.assertIn("docker network create", helper)
        self.assertIn('--driver "$CTFCTL_CHALLENGE_NETWORK_DRIVER"', helper)
        self.assertIn('--subnet "$CTFCTL_CHALLENGE_NETWORK_SUBNET"', helper)
        self.assertIn('ctfctl_assert_challenge_network_contract', helper)
        self.assertNotIn("docker network rm", helper)
        self.assertNotIn("--label", helper)

        self.assertIn("ctfctl_prepare_default_dynamic_flag_runtime_dir", install)
        self.assertIn("ctfctl_ensure_challenge_network", install)
        self.assertLess(
            install.index("ctfctl_prepare_default_dynamic_flag_runtime_dir"),
            install.index("ctfctl_ensure_challenge_network"),
        )
        self.assertLess(
            install.index("ctfctl_ensure_challenge_network"),
            install.index('ctfctl_ok "INSTALL_COMPLETE"'),
        )

    def test_preflight_deploy_and_rollback_are_read_only_network_consumers(self) -> None:
        preflight = function_body(
            self.ctfctl,
            "ctfctl_preflight() {",
            "ctfctl_backup() {",
        )
        deploy = function_body(
            self.ctfctl,
            "ctfctl_deploy() {",
            "ctfctl_rollback() {",
        )
        rollback = function_body(
            self.ctfctl,
            "ctfctl_rollback() {",
            "ctfctl_smoke_test() {",
        )

        for command_body in (preflight, deploy, rollback):
            self.assertIn("ctfctl_assert_challenge_network_contract", command_body)
            self.assertNotIn("ctfctl_ensure_challenge_network", command_body)
            self.assertNotIn("docker network create", command_body)
            self.assertNotIn("docker network rm", command_body)

        self.assertNotIn("docker network rm", self.ctfctl)


if __name__ == "__main__":
    unittest.main()
