"""Static contracts for fail-closed instance and build revision checks.

Go is not installed in this Windows workspace, so this suite guards the source
contracts until Go unit tests can run in the designated Linux verification
environment. It does not exercise Docker, Redis, Learn, or the proxy at runtime.
"""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_ROOT = PROJECT_ROOT / "services" / "env-orchestrator"


class EnvironmentInstanceGuardContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = (ENV_ROOT / "internal" / "container" / "manager.go").read_text(
            encoding="utf-8"
        )
        self.builder = (ENV_ROOT / "internal" / "container" / "builder.go").read_text(
            encoding="utf-8"
        )
        self.main = (ENV_ROOT / "cmd" / "main.go").read_text(encoding="utf-8")

    def test_instance_metadata_and_current_state_are_required(self) -> None:
        self.assertIn('"environment_revision"', self.manager)
        self.assertIn('"environment_verified_image_id"', self.manager)
        self.assertIn("func (m *Manager) requireCurrentInstanceEnvironment", self.manager)
        self.assertIn("func validateCurrentInstanceEnvironment", self.manager)
        self.assertIn("!envInfo.EnvironmentReady", self.manager)
        self.assertIn("envInfo.EnvironmentRevision != storedRevision", self.manager)
        self.assertIn(
            "envInfo.EnvironmentVerifiedImageID != storedImageID", self.manager
        )
        self.assertGreaterEqual(
            self.manager.count("m.requireCurrentInstanceEnvironment(ctx, instanceID, data)"),
            2,
        )

    def test_creation_and_resolve_check_docker_image_identity(self) -> None:
        self.assertIn("inspect.Image != envInfo.EnvironmentVerifiedImageID", self.manager)
        self.assertIn("expectedImageID != \"\" && inspect.Image != expectedImageID", self.manager)
        self.assertIn("probeContainerPort", self.manager)
        self.assertIn('failure_code\", \"port_unreachable', self.manager)
        self.assertIn('Code: "port_unreachable"', self.manager)
        self.assertIn(
            "m.cleanupStartedContainer(ctx, resp.ID, hostPort, instanceID", self.manager
        )
        self.assertIn("m.cleanupUnavailableInstance(ctx, instanceID, data,", self.manager)
        inspect_branch = self.manager[
            self.manager.index('inspect, inspectErr := m.docker.ContainerInspect(ctx, data["container_id"])'):
            self.manager.index("if inspect.Image != expectedImageID")
        ]
        self.assertIn("m.cleanupUnavailableInstance(ctx, instanceID, data,", inspect_branch)

    def test_downstream_error_bodies_are_not_forwarded_from_environment_fetch(self) -> None:
        fetch_start = self.manager.index("func (m *Manager) fetchChallengeEnv")
        fetch_end = self.manager.index("func instanceEnvironmentMetadata")
        fetch_source = self.manager[fetch_start:fetch_end]
        self.assertNotIn("io.ReadAll(resp.Body)", fetch_source)
        self.assertIn('Code: "environment_verification_required"', fetch_source)
        self.assertIn('Code: "environment_unavailable"', fetch_source)
        self.assertIn("func createInstanceErrorResponse", self.main)
        self.assertNotIn('response := gin.H{"error": err.Error()}', self.main)

    def test_failed_verifications_use_null_image_and_build_updates_bind_source_revision(self) -> None:
        self.assertIn("record.ImageID = nil", self.manager)
        self.assertIn("record.Status == \"ready\" && (record.ImageID == nil", self.manager)
        self.assertIn("SourceRevision int", self.builder)
        self.assertIn('json:"source_revision"', self.builder)
        self.assertIn("updateDockerImage(buildCtx, challengeID, imageName, sourceRevision)", self.builder)
        self.assertIn("mergeBuildFilesWithLegacyDockerfile", self.builder)
        self.assertIn('files["Dockerfile"] = []byte(legacyDockerfile.DockerfileContent)', self.builder)
        self.assertIn('Code: "stale_environment_revision"', self.builder)

    def test_failed_verification_audit_keeps_safe_image_id_and_generic_failure_code(self) -> None:
        failure_start = self.manager.index("func verificationFailureCode")
        failure_end = self.manager.index("// recordFailedEnvironmentVerification")
        failure_source = self.manager[failure_start:failure_end]
        self.assertIn('code := "verification_execution_failed"', failure_source)

        record_start = self.manager.index(
            "func (m *Manager) recordFailedEnvironmentVerification"
        )
        record_end = self.manager.index("// requireCompetitionEnvironmentAccess")
        record_source = self.manager[record_start:record_end]
        self.assertIn("imageID string", record_source)
        self.assertIn("strings.TrimSpace(imageID)", record_source)
        self.assertIn("ImageID:        imageIDPtr", record_source)


if __name__ == "__main__":
    unittest.main()
