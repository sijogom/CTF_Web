"""Static contract for detecting Env-orchestrator Compose-project drift.

The test reads the shell source only. It does not inspect Docker, start a
container, or read an environment file.
"""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def function_body(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


class RuntimeProjectDriftContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = (
            PROJECT_ROOT / "infra" / "scripts" / "lib" / "ctfctl-common.sh"
        ).read_text(encoding="utf-8")

    def test_runtime_guard_is_read_only_and_checks_fixed_project_and_service(self) -> None:
        body = function_body(
            self.source,
            "ctfctl_assert_env_orchestrator_runtime_project_contract() {",
            "ctfctl_ensure_challenge_network() {",
        )
        self.assertIn("docker ps -a", body)
        self.assertIn("com.docker.compose.service=env-orchestrator", body)
        self.assertIn("com.docker.compose.project", body)
        self.assertIn('"$CTFCTL_PROJECT_NAME"', body)
        self.assertIn('"env-orchestrator"', body)
        self.assertNotIn("docker rm", body)
        self.assertNotIn("docker restart", body)
        self.assertNotIn("docker stop", body)
        self.assertNotIn("docker network", body)

    def test_all_lifecycle_commands_guard_before_stateful_or_writer_operations(self) -> None:
        boundaries = (
            ("ctfctl_preflight() {", "ctfctl_backup() {"),
            ("ctfctl_backup() {", "ctfctl_restore() {"),
            ("ctfctl_migrate() {", "ctfctl_deploy() {"),
            ("ctfctl_deploy() {", "ctfctl_rollback() {"),
            ("ctfctl_rollback() {", "ctfctl_smoke_test() {"),
        )
        for start, end in boundaries:
            body = function_body(self.source, start, end)
            call = "ctfctl_assert_env_orchestrator_runtime_project_contract"
            self.assertIn(call, body, start)
            guard_index = body.index(call)
            for operation in (
                "ctfctl_require_service_running",
                "ctfctl_require_service_stopped",
            ):
                if operation in body:
                    self.assertLess(guard_index, body.index(operation), start)


if __name__ == "__main__":
    unittest.main()
