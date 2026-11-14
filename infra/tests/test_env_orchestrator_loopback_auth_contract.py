"""Static contract for the host-network Env API authentication boundary.

The checks intentionally do not start Docker, Caddy, or the Go service. They
only prevent source regressions that would expose the host-network Env API or
reintroduce raw identity-header authentication.
"""

from pathlib import Path
import re
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def service_block(compose: str, service_name: str) -> str:
    match = re.search(
        rf"^  {re.escape(service_name)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:|\Z)",
        compose,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"Compose service is missing: {service_name}")
    return match.group("body")


class EnvOrchestratorLoopbackAuthContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.infra = PROJECT_ROOT / "infra"
        self.main = (
            PROJECT_ROOT / "services" / "env-orchestrator" / "cmd" / "main.go"
        ).read_text(encoding="utf-8")
        self.middleware = (
            PROJECT_ROOT
            / "services"
            / "env-orchestrator"
            / "internal"
            / "auth"
            / "middleware.go"
        ).read_text(encoding="utf-8")
        self.dev = (self.infra / "docker-compose.dev.yml").read_text(encoding="utf-8")
        self.prod = (self.infra / "docker-compose.prod.yml").read_text(encoding="utf-8")

    def test_host_network_overrides_bind_env_api_to_loopback(self) -> None:
        for name, compose in (("development", self.dev), ("production", self.prod)):
            environment = service_block(compose, "env-orchestrator")
            self.assertIn("network_mode: host", environment, name)
            self.assertIn("LISTEN_ADDR: 127.0.0.1:8200", environment, name)

        self.assertIn('getEnv("LISTEN_ADDR", ":8200")', self.main)
        self.assertIn("Addr:              listenAddr", self.main)

    def test_raw_identity_headers_cannot_authenticate_a_direct_request(self) -> None:
        self.assertIn('authHeader := c.GetHeader("Authorization")', self.middleware)
        self.assertIn('accessCookie, _ := c.Cookie("access_token")', self.middleware)
        self.assertIn('c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "인증이 필요합니다"})', self.middleware)
        self.assertNotIn('userID := c.GetHeader("X-User-ID")\n\t\tif userID != ""', self.middleware)
        self.assertIn("X-User-ID 폴백 불허", self.middleware)


if __name__ == "__main__":
    unittest.main()
