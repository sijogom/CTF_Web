"""Static host-bind and fail-closed deployment checks for dynamic flags.

These tests deliberately do not read an environment file, contact Docker, or
create the Linux runtime directory. They keep the source contract aligned until
the Ubuntu validation stage can execute it for real.
"""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = "/var/lib/ctf-web/dynamic-flags"


class DynamicFlagDeploymentContractTests(unittest.TestCase):
    def setUp(self) -> None:
        infra = PROJECT_ROOT / "infra"
        self.base_compose = (infra / "docker-compose.yml").read_text(encoding="utf-8")
        self.dev_compose = (infra / "docker-compose.dev.yml").read_text(encoding="utf-8")
        self.prod_compose = (infra / "docker-compose.prod.yml").read_text(encoding="utf-8")
        self.dynamic_compose = (infra / "docker-compose.dynamic-flags.yml").read_text(
            encoding="utf-8"
        )
        self.env_schema = (infra / ".env.schema").read_text(encoding="utf-8")
        self.ctfctl = (infra / "scripts" / "lib" / "ctfctl-common.sh").read_text(
            encoding="utf-8"
        )
        self.server = (infra / "scripts" / "server.sh").read_text(encoding="utf-8")
        self.env_dockerfile = (
            PROJECT_ROOT / "services" / "env-orchestrator" / "Dockerfile"
        ).read_text(encoding="utf-8")

    def test_host_runtime_bind_is_available_for_dynamic_and_legacy_flag_healthchecks(self) -> None:
        setting = f"DYNAMIC_FLAG_RUNTIME_DIR: ${{DYNAMIC_FLAG_RUNTIME_DIR:-{RUNTIME_DIR}}}"
        source = f"source: ${{DYNAMIC_FLAG_RUNTIME_DIR:-{RUNTIME_DIR}}}"
        target = f"target: ${{DYNAMIC_FLAG_RUNTIME_DIR:-{RUNTIME_DIR}}}"

        for compose in (self.base_compose, self.dev_compose, self.prod_compose):
            self.assertIn(setting, compose)
            self.assertIn("- type: bind", compose)
            self.assertIn(source, compose)
            self.assertIn(target, compose)
            self.assertIn("create_host_path: false", compose)
        self.assertIn("- type: bind", self.dynamic_compose)
        self.assertIn(source, self.dynamic_compose)
        self.assertIn(target, self.dynamic_compose)
        self.assertIn("create_host_path: false", self.dynamic_compose)
        self.assertIn(
            'DYNAMIC_FLAG_RUNTIME_DIR=string @default("/var/lib/ctf-web/dynamic-flags")',
            self.env_schema,
        )

    def test_learn_receives_a_disabled_by_default_secret_keyring_contract(self) -> None:
        self.assertIn(
            "DYNAMIC_INSTANCE_FLAGS_ENABLED: ${DYNAMIC_INSTANCE_FLAGS_ENABLED:-false}",
            self.base_compose,
        )
        self.assertIn(
            "DYNAMIC_FLAG_HMAC_KEYRING: ${DYNAMIC_FLAG_HMAC_KEYRING:-}",
            self.base_compose,
        )
        self.assertIn(
            "DYNAMIC_INSTANCE_FLAGS_ENABLED=boolean @default(false)",
            self.env_schema,
        )
        self.assertIn("DYNAMIC_FLAG_HMAC_KEYRING=string @sensitive", self.env_schema)

    def test_deploy_tool_creates_and_rechecks_a_managed_runtime_directory(self) -> None:
        self.assertIn('CTFCTL_DEFAULT_DYNAMIC_FLAG_RUNTIME_DIR="${CTFCTL_DYNAMIC_FLAG_RUNTIME_ROOT}/dynamic-flags"', self.ctfctl)
        self.assertIn('CTFCTL_ENV_ORCHESTRATOR_RUNTIME_UID="1001"', self.ctfctl)
        self.assertIn('CTFCTL_ENV_ORCHESTRATOR_RUNTIME_GID="1001"', self.ctfctl)
        self.assertIn("adduser -D -u 1001 appuser", self.env_dockerfile)
        self.assertIn("ctfctl_dynamic_flag_runtime_dir_from_env()", self.ctfctl)
        self.assertIn("ctfctl_require_dynamic_flag_runtime_dir()", self.ctfctl)
        self.assertIn("ctfctl_assert_dynamic_flag_runtime_contract()", self.ctfctl)
        self.assertIn("ctfctl_dynamic_flag_feature_is_enabled()", self.ctfctl)
        self.assertIn("ctfctl_assert_dynamic_flag_start_contract()", self.ctfctl)
        self.assertIn("ctfctl_prepare_default_dynamic_flag_runtime_dir()", self.ctfctl)
        self.assertIn("ctfctl_check_dynamic_flag_feature_config()", self.ctfctl)
        self.assertIn('"infra/docker-compose.dynamic-flags.yml"', self.ctfctl)
        self.assertIn('"-f" "$release/infra/docker-compose.dynamic-flags.yml"', self.ctfctl)
        self.assertIn("install -d -m 0700 -o", self.ctfctl)
        self.assertIn('[[ "$mode" == "700" ]]', self.ctfctl)
        self.assertIn("value was not printed", self.ctfctl)

    def test_managed_paths_check_the_runtime_only_when_dynamic_flags_are_enabled(self) -> None:
        preflight = self.ctfctl.split("ctfctl_preflight() {", 1)[1].split(
            "ctfctl_backup() {", 1
        )[0]
        backup = self.ctfctl.split("ctfctl_backup() {", 1)[1].split(
            "ctfctl_restore() {", 1
        )[0]
        restore = self.ctfctl.split("ctfctl_restore() {", 1)[1].split(
            "ctfctl_migrate() {", 1
        )[0]
        migrate = self.ctfctl.split("ctfctl_migrate() {", 1)[1].split(
            "ctfctl_deploy() {", 1
        )[0]
        deploy = self.ctfctl.split("ctfctl_deploy() {", 1)[1].split(
            "ctfctl_rollback() {", 1
        )[0]
        rollback = self.ctfctl.split("ctfctl_rollback() {", 1)[1].split(
            "ctfctl_smoke_test() {", 1
        )[0]
        install = self.ctfctl.split("ctfctl_install() {", 1)[1].split(
            "ctfctl_init_jwt_secrets() {", 1
        )[0]

        for command_body in (preflight, backup, restore, migrate, deploy, rollback):
            self.assertIn('ctfctl_assert_dynamic_flag_start_contract "$env_file"', command_body)
        self.assertIn("ctfctl_prepare_default_dynamic_flag_runtime_dir", install)

        compose_args = self.ctfctl.split("ctfctl_compose_args() {", 1)[1].split(
            "ctfctl_compose() {", 1
        )[0]
        self.assertIn('if ctfctl_dynamic_flag_feature_is_enabled "$env_file"; then', compose_args)
        self.assertIn('printf \'%s\\0\' "-f" "$release/infra/docker-compose.dynamic-flags.yml"', compose_args)

    def test_server_start_checks_the_runtime_contract_without_creating_paths(self) -> None:
        start_docker = self.server.split("start_docker() {", 1)[1].split(
            "stop_docker() {", 1
        )[0]

        self.assertIn('DYNAMIC_FLAG_RUNTIME_ROOT="/var/lib/ctf-web"', self.server)
        self.assertIn('DYNAMIC_FLAG_RUNTIME_UID="1001"', self.server)
        self.assertIn('DYNAMIC_FLAG_RUNTIME_GID="1001"', self.server)
        self.assertIn('DYNAMIC_FLAG_COMPOSE_FILE="$INFRA_DIR/docker-compose.dynamic-flags.yml"', self.server)
        self.assertIn("ensure_dynamic_flag_runtime_contract()", self.server)
        self.assertIn("자동 생성하지 않았습니다", self.server)
        self.assertIn('compose_cmd="$compose_cmd -f $DYNAMIC_FLAG_COMPOSE_FILE"', start_docker)
        self.assertLess(
            start_docker.index("ensure_dynamic_flag_runtime_contract"),
            start_docker.index("$compose_cmd up -d"),
        )


if __name__ == "__main__":
    unittest.main()
