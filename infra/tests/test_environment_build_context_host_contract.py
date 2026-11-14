"""Static contract for generic Docker build-context access in host-network mode.

This suite reads only Compose and ctfctl source. It does not start Docker, WSL,
MinIO, or a challenge container, so it cannot prove runtime DNS or presigned URL
reachability.
"""

from pathlib import Path
import re
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def service_block(compose: str, service_name: str) -> str:
    match = re.search(
        rf"^  {re.escape(service_name)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:|\Z)",
        compose,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"Compose service is missing: {service_name}")
    return match.group("body")


class EnvironmentBuildContextHostContractTests(unittest.TestCase):
    def setUp(self) -> None:
        infra = PROJECT_ROOT / "infra"
        self.base_compose = (infra / "docker-compose.yml").read_text(encoding="utf-8")
        self.dev_compose = (infra / "docker-compose.dev.yml").read_text(
            encoding="utf-8"
        )
        self.production_compose = (infra / "docker-compose.prod.yml").read_text(
            encoding="utf-8"
        )
        self.ctfctl = (infra / "scripts" / "lib" / "ctfctl-common.sh").read_text(
            encoding="utf-8"
        )

    def test_host_network_env_keeps_the_signed_minio_hostname_in_dev_and_production(
        self,
    ) -> None:
        minio = service_block(self.base_compose, "minio")
        self.assertIn('"127.0.0.1:9000:9000"', minio)

        for name, compose in (
            ("development", self.dev_compose),
            ("production", self.production_compose),
        ):
            environment = service_block(compose, "env-orchestrator")
            self.assertIn("network_mode: host", environment, name)
            self.assertIn("extra_hosts:", environment, name)
            self.assertIn('"minio=127.0.0.1"', environment, name)
            self.assertLess(
                environment.index("extra_hosts:"),
                environment.index('"minio=127.0.0.1"'),
                name,
            )

        production_environment = service_block(
            self.production_compose, "env-orchestrator"
        )
        self.assertNotIn("MINIO_REWRITE_HOST", production_environment)

    def test_ctfctl_checks_immutable_release_source_before_transitions(self) -> None:
        assertion_start = self.ctfctl.index(
            "ctfctl_assert_env_orchestrator_build_context_host_contract() {"
        )
        assertion_end = self.ctfctl.index("ctfctl_minio_anonymous_status_code() {")
        assertion = self.ctfctl[assertion_start:assertion_end]

        self.assertIn("ctfctl_require_immutable_release_file", assertion)
        self.assertIn('"$release/infra/docker-compose.yml"', assertion)
        self.assertIn('"$release/infra/docker-compose.prod.yml"', assertion)
        self.assertIn("MINIO_LOOPBACK_PUBLISH_MISSING", assertion)
        self.assertIn("ENV_MINIO_HOST_MAPPING_MISSING", assertion)
        self.assertIn("ENV_MINIO_HOST_MAPPING_MISMATCH", assertion)
        self.assertIn("MINIO_PRESIGNED_URL_REWRITE_FORBIDDEN", assertion)

        command_boundaries = (
            (
                "ctfctl_preflight() {",
                "ctfctl_backup() {",
                'ctfctl_quiet_compose "$release" "$env_file" config --no-interpolate -q',
            ),
            (
                "ctfctl_deploy() {",
                "ctfctl_rollback() {",
                'ctfctl_quiet_compose "$release" "$env_file" up -d --no-build',
            ),
            (
                "ctfctl_rollback() {",
                "ctfctl_smoke_test() {",
                'ctfctl_quiet_compose "$release" "$env_file" up -d --no-build',
            ),
        )
        for start, end, transition in command_boundaries:
            command = self.ctfctl.split(start, 1)[1].split(end, 1)[0]
            call = 'ctfctl_assert_env_orchestrator_build_context_host_contract "$release"'
            self.assertIn(call, command)
            self.assertLess(command.index(call), command.index(transition))


if __name__ == "__main__":
    unittest.main()
