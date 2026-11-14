"""Static deployment-contract checks without Docker, environment files, or live data."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class PersistenceContractTests(unittest.TestCase):
    def test_ctfd_uploads_have_a_dedicated_initialized_named_volume(self) -> None:
        compose = (PROJECT_ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("  ctfd-upload-init:\n", compose)
        self.assertIn('user: "0:0"', compose)
        self.assertIn('chown -R 1001:1001 /var/uploads && chmod 0750 /var/uploads', compose)
        self.assertIn("      - ctfd_uploads:/var/uploads", compose)
        self.assertIn("      UPLOAD_FOLDER: /var/uploads", compose)
        self.assertIn("      ctfd-upload-init:\n        condition: service_completed_successfully", compose)
        self.assertIn("  ctfd_uploads:\n", compose)

    def test_backup_contract_binds_ctfd_database_upload_mount_and_all_archives(self) -> None:
        compose = (PROJECT_ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
        ctfctl = (PROJECT_ROOT / "infra" / "scripts" / "lib" / "ctfctl-common.sh").read_text(encoding="utf-8")

        self.assertIn("  postgres_data:\n", compose)
        for logical_volume in ("redis_data", "minio_data", "caddy_data", "caddy_config", "ctfd_uploads"):
            self.assertIn(f'"{logical_volume}"', ctfctl)
        self.assertIn('POSTGRES_BACKUP_MODE"', ctfctl)
        self.assertIn('CTFD_DATABASE"', ctfctl)
        self.assertIn('CTFD_DATABASE_COMPOSE_MISMATCH', ctfctl)
        self.assertIn('CTFD_UPLOAD_MOUNT"', ctfctl)
        self.assertIn('PERSISTENT_VOLUME_LOGICAL_NAMES"', ctfctl)
        self.assertIn('CTFD_DATABASE_DUMP_MISSING', ctfctl)
        self.assertIn('"ctfd_database": "%s"', ctfctl)
        self.assertIn('"ctfd_upload_volume": "%s"', ctfctl)
        self.assertIn('"ctfd_upload_mount": "%s"', ctfctl)

    def test_ctfd_migration_stays_fail_closed_without_reviewed_upstream_evidence(self) -> None:
        compose = (PROJECT_ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
        ctfd_dockerfile = (PROJECT_ROOT / "ctfd" / "Dockerfile").read_text(encoding="utf-8")
        ctfctl = (PROJECT_ROOT / "infra" / "scripts" / "lib" / "ctfctl-common.sh").read_text(encoding="utf-8")
        ctfd_runtime = compose.split("  ctfd:\n", 1)[1].split("  # === 인증 서비스 ===", 1)[0]

        self.assertIn("FROM ctfd/ctfd:3.7.3", ctfd_dockerfile)
        self.assertNotIn("ENTRYPOINT", ctfd_dockerfile)
        self.assertNotIn("CMD", ctfd_dockerfile)
        self.assertNotIn("  ctfd-migrate:\n", compose)
        self.assertNotIn("entrypoint:", ctfd_runtime)
        self.assertIn('command: ["gunicorn", "CTFd:create_app()"', ctfd_runtime)
        self.assertIn('required_jobs=("auth-migrate" "learn-migrate" "community-migrate")', ctfctl)
        self.assertIn("ctfctl_require_ctfd_runtime_migration_evidence()", ctfctl)
        self.assertIn('CTFD_MIGRATION_EVIDENCE_REQUIRED', ctfctl)
        self.assertIn('for job in auth-migrate learn-migrate community-migrate; do', ctfctl)
        migration_body = ctfctl.split("ctfctl_migrate() {", 1)[1].split("ctfctl_deploy() {", 1)[0]
        self.assertLess(
            migration_body.index('ctfctl_assert_full_migration_contract "$release"'),
            migration_body.index("for job in auth-migrate learn-migrate community-migrate; do"),
        )

    def test_ctfd_evidence_gate_prevents_rollback_restore_or_runtime_start(self) -> None:
        ctfctl = (PROJECT_ROOT / "infra" / "scripts" / "lib" / "ctfctl-common.sh").read_text(encoding="utf-8")
        rollback_body = ctfctl.split("ctfctl_rollback() {", 1)[1].split("ctfctl_smoke_test() {", 1)[0]

        gate_index = rollback_body.index("ctfctl_require_ctfd_runtime_migration_evidence")
        self.assertLess(gate_index, rollback_body.index('if [[ -n "$requested_restore_bundle" ]]'))
        self.assertLess(gate_index, rollback_body.index('ctfctl_restore "$bundle_dir"'))
        self.assertLess(gate_index, rollback_body.index('ctfctl_quiet_compose "$release" "$env_file" up -d --no-build'))
        self.assertLess(gate_index, rollback_body.index('ctfctl_activate_release "$release"'))
        self.assertIn("CTFD_MIGRATION_EVIDENCE_REQUIRED", ctfctl)

    def test_preflight_and_release_changes_require_manifest_and_pinned_images(self) -> None:
        ctfctl = (PROJECT_ROOT / "infra" / "scripts" / "lib" / "ctfctl-common.sh").read_text(encoding="utf-8")

        self.assertIn('CTFCTL_RELEASE_MANIFEST_RELATIVE_PATH="infra/release-manifest.json"', ctfctl)
        self.assertIn('CTFCTL_RELEASE_FILES_RELATIVE_PATH="infra/release-files.sha256"', ctfctl)
        self.assertIn("ctfctl_assert_release_manifest_contract()", ctfctl)
        self.assertIn("ctfctl_assert_release_files_contract()", ctfctl)
        self.assertIn("ctfctl_assert_immutable_image_contract()", ctfctl)
        self.assertIn("IMAGE_DIGESTS_MISSING", ctfctl)
        self.assertIn("RELEASE_RUNTIME_FILE_UNMANIFESTED", ctfctl)
        self.assertIn('"--project-name" "$CTFCTL_PROJECT_NAME"', ctfctl)

        preflight_body = ctfctl.split("ctfctl_preflight() {", 1)[1].split("ctfctl_backup() {", 1)[0]
        self.assertIn('ctfctl_assert_release_manifest_contract "$release"', preflight_body)
        self.assertIn('ctfctl_assert_immutable_image_contract "$release"', preflight_body)
        self.assertNotIn('ctfctl_activate_release', preflight_body)
        self.assertNotIn('ctfctl_compose "$release" "$env_file" up', preflight_body)

    def test_production_secret_gate_rejects_base_compose_placeholders_without_logging_values(self) -> None:
        ctfctl = (PROJECT_ROOT / "infra" / "scripts" / "lib" / "ctfctl-common.sh").read_text(encoding="utf-8")
        self.assertIn("ctfctl_env_has_insecure_default()", ctfctl)
        self.assertIn("SECRET_REFERENCE_INSECURE_DEFAULT", ctfctl)
        self.assertIn("ctfd-dev-secret-change-me", ctfctl)
        self.assertIn("127\\\\.0\\\\.0\\\\.1", ctfctl)
        secret_gate = ctfctl.split("ctfctl_check_secret_references() {", 1)[1].split("ctfctl_compose_args() {", 1)[0]
        self.assertIn('ctfctl_env_has_insecure_default "$env_file" "$key"', secret_gate)
        self.assertNotIn('printf "%s" "$value"', secret_gate)


if __name__ == "__main__":
    unittest.main()
