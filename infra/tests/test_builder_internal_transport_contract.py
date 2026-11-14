"""Static contract for internal Docker build URL transport isolation."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class BuilderInternalTransportContractTests(unittest.TestCase):
    def test_build_transport_does_not_use_environment_proxy(self) -> None:
        source = (
            PROJECT_ROOT
            / "services"
            / "env-orchestrator"
            / "internal"
            / "container"
            / "builder.go"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "transport := http.DefaultTransport.(*http.Transport).Clone()",
            source,
        )
        self.assertIn("transport.Proxy = nil", source)
        self.assertIn("Transport: transport", source)


if __name__ == "__main__":
    unittest.main()
