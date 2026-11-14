"""Worker wiring must preserve the service security and recovery boundaries."""
from pathlib import Path
import unittest

import yaml


class ComposeLoader(yaml.SafeLoader):
    pass


ComposeLoader.add_constructor("!reset", lambda loader, node: loader.construct_sequence(node))


class RenewalWorkerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.base = yaml.load((root / "docker-compose.yml").read_text(encoding="utf-8"), Loader=ComposeLoader)["services"]
        cls.prod = yaml.load((root / "docker-compose.prod.yml").read_text(encoding="utf-8"), Loader=ComposeLoader)["services"]

    def test_workers_share_their_canonical_build_context(self):
        for worker, api in (("personal-notification-runner", "auth-service"),
                            ("marketplace-settlement-runner", "community-service"),
                            ("comment-notification-runner", "community-service")):
            with self.subTest(worker=worker):
                self.assertEqual(self.base[worker]["build"], self.base[api]["build"])
                self.assertNotIn("ports", self.base[worker])
                self.assertNotIn("volumes", self.base[worker])
                self.assertIn("no-new-privileges:true", self.prod[worker]["security_opt"])
                self.assertEqual(self.prod[worker]["cap_drop"], ["ALL"])
                self.assertTrue(self.prod[worker]["read_only"])
                self.assertIn("/ready", self.base[worker]["healthcheck"]["test"][-1])

    def test_auth_worker_gets_only_file_backed_jwt_and_existing_smtp_policy(self):
        worker = self.base["personal-notification-runner"]
        env = worker["environment"]
        self.assertIn("auth_jwt_private_key", worker["secrets"])
        self.assertIn("platform_jwt_public_key", worker["secrets"])
        self.assertNotIn("JWT_SECRET_KEY", env)
        self.assertEqual(env["JWT_SECRET_KEY_FILE"], self.base["auth-service"]["environment"]["JWT_SECRET_KEY_FILE"])
        for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_FROM", "SMTP_TLS", "SMTP_USER", "SMTP_PASSWORD"):
            self.assertEqual(env[key], self.base["auth-service"]["environment"][key])
        self.assertEqual(self.prod["personal-notification-runner"]["cap_add"], self.prod["auth-service"]["cap_add"])

    def test_community_workers_wait_for_migrations_and_use_internal_auth(self):
        for name in ("marketplace-settlement-runner", "comment-notification-runner"):
            worker = self.base[name]
            self.assertEqual(worker["depends_on"]["community-migrate"]["condition"], "service_completed_successfully")
            self.assertEqual(worker["environment"]["AUTH_SERVICE_URL"], "http://auth-service:8020")
            self.assertIn("INTERNAL_API_KEY", worker["environment"])
            self.assertEqual(worker["secrets"], ["platform_jwt_public_key"])
            self.assertNotIn("cap_add", self.prod[name])


if __name__ == "__main__":
    unittest.main()
