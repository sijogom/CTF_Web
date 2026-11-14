"""Deployment sources must not carry unexplained database credentials."""
import ast
from pathlib import Path
import unittest
from urllib.parse import urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[2]


class DatabaseDefaultsContractTests(unittest.TestCase):
    def test_service_defaults_are_explicit_placeholders(self):
        for service in ("learn", "community"):
            with self.subTest(service=service):
                tree = ast.parse((ROOT / "services" / service / "app" / "config.py").read_text(encoding="utf-8"))
                settings = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Settings")
                field = next(node for node in settings.body if isinstance(node, ast.AnnAssign)
                             and isinstance(node.target, ast.Name) and node.target.id == "database_url")
                self.assertEqual(urlsplit(ast.literal_eval(field.value)).password, "CHANGE_ME")

    def test_api_and_migration_receive_the_configured_database_url(self):
        model = yaml.safe_load((ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8"))
        for service in ("learn", "community"):
            for name in (service + "-service", service + "-migrate"):
                with self.subTest(service=name):
                    value = model["services"][name]["environment"]["DATABASE_URL"]
                    self.assertIn("${" + service.upper() + "_DB_PASSWORD", value)
                    self.assertIn("@postgres:5432/" + service + "_db", value)


if __name__ == "__main__":
    unittest.main()
