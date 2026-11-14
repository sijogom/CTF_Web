import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
MANAGER = (ROOT / "services" / "env-orchestrator" / "internal" / "container" / "image_cleanup.go").read_text(encoding="utf-8")
MAIN = (ROOT / "services" / "env-orchestrator" / "cmd" / "main.go").read_text(encoding="utf-8")
BASE = (ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
PROD = (ROOT / "infra" / "docker-compose.prod.yml").read_text(encoding="utf-8")


class ChallengeImageCleanupContractTests(unittest.TestCase):
    def test_cleanup_is_exact_tag_and_container_safe(self):
        self.assertIn('fmt.Sprintf("ctf-challenge-%d:latest", challengeID)', MANAGER)
        self.assertIn('challenge-id=%d', MANAGER)
        self.assertIn("All:     true", MANAGER)
        self.assertIn("ErrChallengeImageInUse", MANAGER)
        self.assertIn("Force:         false", MANAGER)
        self.assertIn("PruneChildren: false", MANAGER)

    def test_internal_key_route_is_fail_closed(self):
        self.assertIn('r.POST("/internal/challenges/:id/cleanup-image"', MAIN)
        self.assertIn("validInternalAPIKey(c, internalAPIKey)", MAIN)
        self.assertIn('http.StatusUnauthorized', MAIN)
        self.assertIn('challenge_image_in_use', MAIN)

    def test_learn_can_reach_host_mode_env(self):
        self.assertIn("ENV_ORCHESTRATOR_URL: http://env-orchestrator:8200", BASE)
        self.assertIn('"host.docker.internal:host-gateway"', PROD)
        self.assertIn("ENV_ORCHESTRATOR_URL: http://host.docker.internal:8200", PROD)

    def test_cleanup_outbox_runner_is_internal_and_migration_ordered(self):
        self.assertIn("challenge-image-cleanup-runner:", BASE)
        self.assertIn(
            'command: ["python", "-m", "app.workers.challenge_image_cleanup_runner"]',
            BASE,
        )
        self.assertIn("learn-migrate:", BASE)
        self.assertIn("env-orchestrator:", BASE)
        self.assertIn("CHALLENGE_IMAGE_CLEANUP_MAX_ATTEMPTS", BASE)
        self.assertIn("challenge-image-cleanup-runner:", PROD)
        runner = PROD[PROD.index("challenge-image-cleanup-runner:") : PROD.index("lesson-image-reconcile-runner:")]
        self.assertIn("network_mode: host", runner)
        self.assertIn("networks: !reset []", runner)
        self.assertIn("DATABASE_URL: postgresql+asyncpg://learn_svc:", runner)
        self.assertIn("ENV_ORCHESTRATOR_URL: http://127.0.0.1:8200", runner)
        self.assertIn("read_only: true", PROD)
        self.assertNotIn("ports:", runner)


if __name__ == "__main__":
    unittest.main()
