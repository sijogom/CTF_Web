"""Static deployment-contract tests that never load environment settings."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class JwtRuntimeSecretContractTests(unittest.TestCase):
    def test_private_key_is_not_copied_into_any_service_image(self) -> None:
        for service in ("auth", "learn", "community"):
            dockerfile = (PROJECT_ROOT / "services" / service / "Dockerfile").read_text(encoding="utf-8")
            self.assertNotIn("COPY jwt_private.pem", dockerfile)
            self.assertNotIn("COPY jwt_public.pem", dockerfile)

            dockerignore = (PROJECT_ROOT / "services" / service / ".dockerignore").read_text(encoding="utf-8")
            self.assertIn("*.pem", dockerignore)
            self.assertIn("*.key", dockerignore)

    def test_auth_runtime_entrypoint_copies_private_key_only_to_tmpfs(self) -> None:
        entrypoint = (PROJECT_ROOT / "services" / "auth" / "docker-entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn('SECRET_SOURCE="/run/secrets/auth_jwt_private_key"', entrypoint)
        self.assertIn('RUNTIME_SECRET_DIR="/run/ctf-runtime-secrets"', entrypoint)
        self.assertIn('install -d -m 0700 "$RUNTIME_SECRET_DIR"', entrypoint)
        self.assertIn('cp "$SECRET_SOURCE" "$RUNTIME_SECRET_FILE"', entrypoint)
        self.assertIn('chmod 0600 "$RUNTIME_SECRET_FILE"', entrypoint)
        self.assertIn('chown appuser:appuser "$RUNTIME_SECRET_FILE"', entrypoint)
        self.assertIn('chown appuser:appuser "$RUNTIME_SECRET_DIR"', entrypoint)
        self.assertIn('exec gosu appuser "$@"', entrypoint)

    def test_compose_uses_file_paths_and_grants_only_required_secrets(self) -> None:
        compose = (PROJECT_ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("JWT_SECRET_KEY_FILE: /run/ctf-runtime-secrets/jwt_private.pem", compose)
        self.assertIn("JWT_PUBLIC_KEY_FILE: /run/secrets/platform_jwt_public_key", compose)
        self.assertIn("file: ${JWT_SECRET_KEY_SOURCE:?", compose)
        self.assertIn("file: ${JWT_PUBLIC_KEY_SOURCE:?", compose)
        self.assertNotIn("JWT_SECRET_KEY: ${JWT_SECRET_KEY:-}", compose)
        self.assertNotIn("JWT_PUBLIC_KEY: ${JWT_PUBLIC_KEY:-}", compose)

        production = (PROJECT_ROOT / "infra" / "docker-compose.prod.yml").read_text(encoding="utf-8")
        auth_service = production.split("  auth-service:", 1)[1].split("\n  learn-service:", 1)[0]
        self.assertIn("cap_add:", auth_service)
        for capability in ("CHOWN", "SETUID", "SETGID"):
            self.assertIn(f"      - {capability}", auth_service)
        self.assertIn("read_only: true", auth_service)
        self.assertIn("- /tmp", auth_service)
        self.assertIn("- /run/ctf-runtime-secrets:uid=0,gid=0,mode=0700", auth_service)

    def test_ctfctl_requires_external_key_sources_and_provisions_without_overwrite(self) -> None:
        script = (PROJECT_ROOT / "infra" / "scripts" / "lib" / "ctfctl-common.sh").read_text(encoding="utf-8")
        command = (PROJECT_ROOT / "infra" / "scripts" / "ctfctl").read_text(encoding="utf-8")
        self.assertIn('readonly CTFCTL_SECRETS_DIR="/etc/ctf-web/secrets"', script)
        self.assertIn('ctfctl_check_jwt_key_sources', script)
        self.assertIn('ctfctl_init_jwt_secrets', script)
        self.assertIn('JWT_KEY_ALREADY_EXISTS', script)
        self.assertIn('ctfctl_create_private_dir_if_missing "$scope_parent"', script)
        self.assertIn('ctfctl_create_private_dir_if_missing "/etc"', script)
        self.assertIn('init-jwt-secrets) ctfctl_command_init_jwt_secrets', command)

    def test_legacy_launchers_cannot_generate_or_copy_source_tree_keys(self) -> None:
        scripts_dir = PROJECT_ROOT / "infra" / "scripts"
        for script_name in ("deploy.sh", "deploy-wsl.sh", "server.sh"):
            script = (scripts_dir / script_name).read_text(encoding="utf-8")
            self.assertNotIn("openssl genpkey -algorithm RSA -out", script)
            self.assertNotIn("services/learn/jwt_public.pem", script)
            self.assertNotIn("services/community/jwt_public.pem", script)
            self.assertIn("소스 폴더 JWT 키 생성은 보안상 중단되었습니다", script)


if __name__ == "__main__":
    unittest.main()
