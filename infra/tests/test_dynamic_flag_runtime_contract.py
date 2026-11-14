"""Static contracts for generic dynamic-instance flag injection.

Go and Docker are unavailable in this Windows workspace. These checks prevent
regression of the source-level security and lifecycle contract until the Linux
VM runs the Go and Docker integration suites.
"""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_ROOT = PROJECT_ROOT / "services" / "env-orchestrator"


class DynamicFlagRuntimeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = (ENV_ROOT / "internal" / "container" / "manager.go").read_text(
            encoding="utf-8"
        )
        self.dynamic = (
            ENV_ROOT / "internal" / "container" / "dynamic_flag.go"
        ).read_text(encoding="utf-8")
        self.main = (ENV_ROOT / "cmd" / "main.go").read_text(encoding="utf-8")
        lifecycle_root = ENV_ROOT / "internal" / "lifecycle"
        self.ttl = (lifecycle_root / "ttl.go").read_text(encoding="utf-8")
        self.reconciler = (lifecycle_root / "reconciler.go").read_text(
            encoding="utf-8"
        )
        self.runtime_cleanup = (lifecycle_root / "runtime_cleanup.go").read_text(
            encoding="utf-8"
        )
        self.learn_internal = (
            PROJECT_ROOT / "services" / "learn" / "app" / "api" / "internal.py"
        ).read_text(encoding="utf-8")

    def test_is_generic_not_gacha_or_port_specific(self) -> None:
        combined = self.manager + self.dynamic + self.main
        self.assertNotIn("gacha", combined.lower())
        self.assertNotIn('"5000"', combined)
        self.assertIn('json:"challenge_id"', self.manager)
        self.assertIn('json:"environment_revision"', self.manager)
        self.assertIn('json:"verified_image_id"', self.manager)

    def test_deployment_smoke_message_is_generic(self) -> None:
        ctfctl = (
            PROJECT_ROOT / "infra" / "scripts" / "lib" / "ctfctl-common.sh"
        ).read_text(encoding="utf-8")
        self.assertNotIn("gacha containers", ctfctl.lower())
        self.assertIn("generic Docker challenge fixtures", ctfctl)

    def test_static_and_regex_paths_do_not_mount_or_issue_a_flag(self) -> None:
        creation_start = self.manager.index("func (m *Manager) CreateInstance")
        creation_end = self.manager.index("// EnvironmentVerificationResult")
        creation = self.manager[creation_start:creation_end]
        self.assertIn("dynamicEnabled := envInfo.DynamicFlag.Enabled", creation)
        self.assertIn("if dynamicEnabled {", creation)
        self.assertIn("m.issueDynamicFlag", creation)
        self.assertIn("applyDynamicFlagRuntime", creation)
        self.assertIn('containerConfig.Env = []string{"FLAG_PATH=" + dynamicRuntimePath}', creation)
        self.assertNotIn("flag.txt", creation.replace("dynamicFlagRuntimePath", ""))

    def test_secret_has_a_host_private_read_only_runtime_boundary(self) -> None:
        self.assertIn("os.CreateTemp", self.dynamic)
        self.assertIn("O_CREATE|O_EXCL", self.dynamic)
        self.assertIn("tmp.Chmod(0o600)", self.dynamic)
        self.assertIn("tmp.Chmod(0o444)", self.dynamic)
        self.assertIn("os.Chmod(dir, 0o700)", self.dynamic)
        self.assertIn("ReadOnly: true", self.dynamic)
        self.assertIn('defaultDynamicFlagDir     = "/var/lib/ctf-web/dynamic-flags"', self.manager)
        self.assertIn("dynamicFlagMaxTTL", self.manager)
        self.assertIn('dynamicFlagRuntimePath    = "/flag.txt"', self.manager)
        self.assertIn("validateDynamicFlagValue", self.dynamic)
        self.assertIn('strings.ContainsAny(instanceID, "/\\\\")', self.dynamic)
        self.assertIn('strings.HasPrefix(instanceID, "verify_")', self.dynamic)
        self.assertIn("dynamic flag sweep rejected a non-absolute runtime directory", self.dynamic)

    def test_static_legacy_healthcheck_uses_non_secret_compatibility_mount(self) -> None:
        creation_start = self.manager.index("func (m *Manager) CreateInstance")
        creation_end = self.manager.index("// EnvironmentVerificationResult")
        creation = self.manager[creation_start:creation_end]
        self.assertIn("imageNeedsCompatibilityFlag", creation)
        self.assertIn("writeCompatibilityFlagFile", creation)
        self.assertIn("compatibilityFlagSource", creation)
        self.assertIn("compatibilityFlagHostPath", self.dynamic)
        self.assertIn('return m.writeRuntimeFlagFile(path, "ok\\n")', self.dynamic)
        self.assertIn("removeCompatibilityFlagFile", self.manager)
        self.assertIn("compatibilityFlagInstanceIDFromFileName", self.dynamic)

    def test_no_raw_flag_is_persisted_or_logged(self) -> None:
        metadata_start = self.manager.index("instanceMetadata :=")
        metadata_end = self.manager.index("if err := m.rdb.HSet", metadata_start)
        self.assertNotIn('"flag"', self.manager[metadata_start:metadata_end])
        self.assertNotIn("issued.Flag", self.manager[metadata_start:metadata_end])
        issue_request_start = self.manager.index("type dynamicFlagIssueRequest")
        issue_request_end = self.manager.index("type dynamicFlagIssueResponse")
        self.assertNotIn("\tFlag ", self.manager[issue_request_start:issue_request_end])
        self.assertIn("issued.Flag = \"\"", self.manager)

    def test_issue_revoke_and_all_cleanup_boundaries_exist(self) -> None:
        self.assertIn('dynamicFlagIssuePath  = "/internal/dynamic-flags/issue"', self.dynamic)
        self.assertIn('dynamicFlagRevokePath = "/internal/dynamic-flags/revoke"', self.dynamic)
        for reason in (
            "container_start_failed",
            "readiness_failed",
            "instance_stopped",
            "instance_expired",
            "environment_drift",
            "reconcile_cleanup",
            "metadata_persist_failed",
        ):
            self.assertIn(f'"{reason}"', self.dynamic)
        for learn_reason in (
            "instance_failed",
            "instance_stopped",
            "instance_expired",
            "environment_stale",
            "cleanup_failed",
        ):
            self.assertIn(f'"{learn_reason}"', self.dynamic)
        self.assertIn("func (m *Manager) cleanupDynamicFlagRuntime", self.manager)
        self.assertIn("func (m *Manager) SweepDynamicFlagRuntime", self.dynamic)
        self.assertIn("manager.SweepDynamicFlagRuntime(ctx)", self.main)

    def test_ttl_and_reconciler_revoke_dynamic_runtime_before_record_removal(self) -> None:
        self.assertIn("type InstanceRuntimeCleanup", self.runtime_cleanup)
        self.assertIn("runInstanceRuntimeCleanup", self.runtime_cleanup)
        self.assertIn("func (m *Manager) CleanupInstanceRuntime", self.manager)
        self.assertIn("m.cleanupDynamicFlagRuntime(instanceID, data, reason)", self.manager)
        self.assertIn("m.cleanupUnrecordedDynamicFlagRuntime(instanceID, reason)", self.manager)
        self.assertIn("func (m *Manager) cleanupUnrecordedDynamicFlagRuntime", self.dynamic)
        self.assertIn("runtimeCleanup InstanceRuntimeCleanup", self.ttl)
        self.assertIn("runtimeCleanup InstanceRuntimeCleanup", self.reconciler)
        self.assertIn(
            'runInstanceRuntimeCleanup(w.runtimeCleanup, instanceID, data, "instance_expired")',
            self.ttl,
        )
        self.assertGreaterEqual(
            self.reconciler.count(
                'runInstanceRuntimeCleanup(r.runtimeCleanup, instanceID, data, "reconcile_cleanup")'
            ),
            3,
        )
        self.assertGreaterEqual(self.main.count("manager.CleanupInstanceRuntime"), 2)

    def test_learn_contract_matches_env_issue_and_revoke_payloads(self) -> None:
        # Decorator options (for example the stricter internal-key dependency) may
        # make this a multi-line decorator. Assert the route argument rather than
        # coupling the contract test to one formatting style.
        self.assertIn('"/dynamic-flags/issue"', self.learn_internal)
        self.assertIn('"/dynamic-flags/revoke"', self.learn_internal)
        for field in (
            "challenge_id",
            "instance_id",
            "user_id",
            "competition_id",
            "environment_revision",
            "verified_image_id",
            "expires_at",
        ):
            self.assertIn(f"{field}:", self.learn_internal)
        for allowlisted_reason in (
            "instance_failed",
            "instance_stopped",
            "instance_expired",
            "environment_stale",
            "cleanup_failed",
        ):
            self.assertIn(f'"{allowlisted_reason}"', self.learn_internal)

    def test_verification_uses_a_probe_not_participant_issuance(self) -> None:
        verify_start = self.manager.index("func (m *Manager) VerifyChallengeEnvironment")
        verify = self.manager[verify_start:]
        self.assertIn('"CTF_DYNAMIC_FLAG_PROBE"', verify)
        self.assertIn("applyDynamicFlagRuntime(verificationHostConfig", verify)
        self.assertNotIn("issueDynamicFlag", verify)

    def test_dynamic_extension_restarts_before_accepting_new_expiry(self) -> None:
        self.assertIn("func (m *Manager) refreshDynamicFlagForExtension", self.dynamic)
        self.assertIn("func dynamicFlagRequestedExpiry", self.dynamic)
        self.assertIn("m.docker.ContainerRestart", self.dynamic)
        self.assertIn("m.waitForContainerReady", self.dynamic)
        self.assertIn("m.refreshDynamicFlagForExtension", self.manager)
        self.assertIn("requestedExpiresAt, err = dynamicFlagRequestedExpiry", self.manager)
        self.assertIn("requestedExpiry, err = dynamicFlagRequestedExpiry", self.dynamic)


if __name__ == "__main__":
    unittest.main()
