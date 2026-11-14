"""Community must not authorize requests from raw X-User-* headers."""

from pathlib import Path
import ast
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPS = PROJECT_ROOT / "services" / "community" / "app" / "api" / "deps.py"
CADDY = PROJECT_ROOT / "infra" / "caddy" / "Caddyfile"


class CommunityIdentityBoundaryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = DEPS.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        cls.caddy = CADDY.read_text(encoding="utf-8")

    def test_only_signed_token_path_is_available(self) -> None:
        self.assertNotIn("from fastapi import Header", self.source)
        self.assertNotRegex(self.source, r"\bx_user_(?:id|role|name)\b")
        self.assertIn("jwt.decode", self.source)
        self.assertIn("_require_current_authorization", self.source)

    def test_required_identity_fails_closed_without_token(self) -> None:
        function = next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "get_current_user_id"
        )
        body = ast.get_source_segment(self.source, function) or ""
        self.assertIn("status.HTTP_401_UNAUTHORIZED", body)
        self.assertNotIn("x_user_id", body)

    def test_caddy_header_copy_is_not_an_application_auth_contract(self) -> None:
        # Caddy may copy forward_auth response headers for legacy observability,
        # but the Community dependency must ignore them and verify the JWT itself.
        self.assertIn("forward_auth", self.caddy)
        self.assertIn("request_header -X-User-ID", self.caddy)
        self.assertNotIn("X-User-ID", self.source)


if __name__ == "__main__":
    unittest.main()
