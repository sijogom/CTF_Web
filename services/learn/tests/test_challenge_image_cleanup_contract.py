import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
API = (ROOT / "app" / "api" / "challenges.py").read_text(encoding="utf-8")
UTILITY = (ROOT / "app" / "utils" / "challenge_image_cleanup.py").read_text(encoding="utf-8")
CONFIG = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
MODEL = (ROOT / "app" / "models" / "challenge_image_cleanup.py").read_text(encoding="utf-8")
WORKER = (ROOT / "app" / "workers" / "challenge_image_cleanup_runner.py").read_text(encoding="utf-8")
MIGRATION = next(
    (ROOT / "migrations" / "versions").glob("20260816_026_challenge_image_cleanup_jobs.py")
).read_text(encoding="utf-8")
MIGRATE_SCRIPT = (ROOT / "app" / "scripts" / "learn_migrate.py").read_text(encoding="utf-8")


class ChallengeImageCleanupContractTests(unittest.TestCase):
    def test_sources_parse(self):
        ast.parse(API)
        ast.parse(UTILITY)
        ast.parse(CONFIG)
        ast.parse(MODEL)
        ast.parse(WORKER)
        ast.parse(MIGRATION)
        ast.parse(MIGRATE_SCRIPT)

    def test_delete_enqueues_exact_platform_tag_in_same_transaction(self):
        self.assertIn("ChallengeImageCleanupJob", API)
        self.assertIn('f"ctf-challenge-{challenge_id}:latest"', API)
        enqueue_at = API.index("ChallengeImageCleanupJob(")
        delete_at = API.index("sa_delete(Challenge).where(Challenge.id == challenge_id)")
        commit_at = API.index("await db.commit()", delete_at)
        self.assertLess(enqueue_at, delete_at)
        self.assertLess(delete_at, commit_at)
        self.assertIn('challenge.docker_image == platform_image_reference', API)
        self.assertIn("image_cleanup_pending", API)

    def test_worker_revalidates_platform_tag_and_retries(self):
        self.assertIn("_validate_image_reference", WORKER)
        self.assertIn("with_for_update(skip_locked=True)", WORKER)
        self.assertIn("CHALLENGE_IMAGE_CLEANUP_MAX_ATTEMPTS", WORKER)
        self.assertIn("_STATUS_PENDING", WORKER)
        self.assertIn("_STATUS_COMPLETED", WORKER)
        self.assertIn("_STATUS_FAILED", WORKER)

    def test_migration_is_after_submission_flag_reveal_and_fail_closed(self):
        self.assertIn('revision: str = "20260816_026"', MIGRATION)
        self.assertIn('down_revision: Union[str, None] = "20260816_025"', MIGRATION)
        self.assertIn('"challenges" not in set(_inspector().get_table_names())', MIGRATION)
        self.assertIn("challenge_image_cleanup_jobs", MIGRATION)
        self.assertIn("app.models.challenge_image_cleanup", MIGRATE_SCRIPT)

    def test_internal_cleanup_does_not_forward_downstream_response_body(self):
        self.assertIn("/cleanup-image", UTILITY)
        self.assertIn('response.status_code == 409', UTILITY)
        self.assertNotIn("response.text", UTILITY)
        self.assertNotIn("response.content", UTILITY)
        self.assertIn('{"removed", "absent"}', UTILITY)

    def test_env_orchestrator_setting_is_explicit(self):
        self.assertIn("env_orchestrator_url", CONFIG)
        self.assertIn("internal_api_key", UTILITY)


if __name__ == "__main__":
    unittest.main()
