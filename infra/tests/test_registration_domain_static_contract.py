"""The cju.ac.kr-only registration boundary must remain dependency-free testable."""

import ast
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTH_ROOT = PROJECT_ROOT.parent / "services" / "auth" / "app"


def _literal_assignment(tree: ast.Module, name: str):
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in statement.targets
        ):
            return ast.literal_eval(statement.value)
    raise AssertionError(f"assignment not found: {name}")


class RegistrationDomainStaticContractTests(unittest.TestCase):
    def test_registration_domain_is_exactly_cju_ac_kr(self) -> None:
        source = (AUTH_ROOT / "schemas" / "auth.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertEqual(_literal_assignment(tree, "ALLOWED_EMAIL_DOMAINS"), ["cju.ac.kr"])
        self.assertIn("domain not in ALLOWED_EMAIL_DOMAINS", source)

    def test_registration_checks_admin_open_setting_before_creating_user(self) -> None:
        source = (AUTH_ROOT / "api" / "register.py").read_text(encoding="utf-8")
        self.assertIn("load_admin_settings", source)
        self.assertIn('"registration_open"', source)
        self.assertIn("HTTP_403_FORBIDDEN", source)
        self.assertLess(source.index("registration_open"), source.index("user = User("))


if __name__ == "__main__":
    unittest.main()
