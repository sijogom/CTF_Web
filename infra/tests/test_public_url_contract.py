"""Production public URL must match the Nginx-to-Caddy HTTPS contract."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROD_COMPOSE = PROJECT_ROOT / "docker-compose.prod.yml"
FRONTEND_DOCKERFILE = PROJECT_ROOT.parent / "frontend" / "Dockerfile"


class PublicUrlContractTests(unittest.TestCase):
    def test_production_frontend_build_uses_https_and_ctf_base_path(self) -> None:
        source = PROD_COMPOSE.read_text(encoding="utf-8")
        self.assertIn(
            "NEXT_PUBLIC_SITE_URL: https://${SITE_DOMAIN:-localhost}/ctf",
            source,
        )
        self.assertNotIn(
            "NEXT_PUBLIC_SITE_URL: http://${SITE_DOMAIN:-localhost}/ctf",
            source,
        )
        self.assertIn("NEXT_PUBLIC_BASE_PATH: /ctf", source)
        self.assertIn(
            'CADDY_SITE_ADDRESS: ":8080"',
            source,
        )

    def test_frontend_default_is_https(self) -> None:
        source = FRONTEND_DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("ARG NEXT_PUBLIC_SITE_URL=https://localhost", source)


if __name__ == "__main__":
    unittest.main()
