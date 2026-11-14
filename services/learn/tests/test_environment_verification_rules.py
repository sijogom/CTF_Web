"""Environment publication-gate rules do not require a DB or Docker daemon."""

import sys
from pathlib import Path
import unittest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.utils.environment_verification import (  # noqa: E402
    detect_dockerfile_exposed_port,
    environment_configuration_changed,
    environment_is_ready,
    environment_is_verified_for_runtime,
    environment_status,
    flag_policy_configuration_changed,
    next_environment_revision,
    normalize_environment_failure_code,
)


class EnvironmentVerificationRuleTests(unittest.TestCase):
    def test_dockerfile_expose_is_only_a_safe_default_port_hint(self) -> None:
        self.assertEqual(
            detect_dockerfile_exposed_port("FROM python:3.12\nEXPOSE 5000\nCMD [\"app\"]"),
            5000,
        )
        self.assertEqual(
            detect_dockerfile_exposed_port("EXPOSE 5000/udp\nEXPOSE 8080/tcp"),
            8080,
        )
        self.assertIsNone(detect_dockerfile_exposed_port("EXPOSE ${PORT}"))

    def test_create_route_uses_expose_only_when_port_was_omitted(self) -> None:
        source = (SERVICE_ROOT / "app" / "api" / "challenges.py").read_text(encoding="utf-8")
        self.assertIn('"container_port" not in body.model_fields_set', source)
        self.assertIn("detect_dockerfile_exposed_port(body.dockerfile_content)", source)
        self.assertIn("container_port=container_port", source)

    def test_patch_route_recomputes_port_when_dockerfile_changes_without_override(self) -> None:
        source = (SERVICE_ROOT / "app" / "api" / "challenges.py").read_text(encoding="utf-8")
        self.assertIn('"dockerfile_content" in update_data', source)
        self.assertIn('"container_port" not in update_data', source)
        self.assertIn("detect_dockerfile_exposed_port(target_dockerfile_content)", source)

    def test_legacy_build_context_upload_uses_generic_expose_hint_only_for_default_port(self) -> None:
        source = (SERVICE_ROOT / "app" / "api" / "challenges.py").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_DOCKER_CONTAINER_PORT = 8080", source)
        self.assertIn('filename == "Dockerfile"', source)
        self.assertIn("detect_dockerfile_exposed_port(\n                    data.decode(\"utf-8\")", source)
        self.assertIn('"container_port": challenge.container_port', source)

    def test_import_route_uses_the_same_generic_expose_fallback(self) -> None:
        from app.services.challenge_imports import plan_import, prepare_import

        source = (SERVICE_ROOT / "app" / "api" / "challenges.py").read_text(encoding="utf-8")
        self.assertIn("return await import_challenge_batch(", source)
        for explicit_port, expected in ((None, 5000), (9090, 9090)):
            with self.subTest(explicit_port=explicit_port):
                record = {"name": "Port fallback", "env_type": "dockerfile", "dockerfile_content": "FROM python\nEXPOSE 5000/tcp"}
                if explicit_port is not None:
                    record["container_port"] = explicit_port
                mode, records = prepare_import({"challenges": [record]})
                targets, _, _, _ = plan_import(records, mode, [], [], [], [])
                self.assertEqual(targets[0].fields["container_port"], expected)

    def test_environment_without_container_never_requires_verification(self) -> None:
        self.assertTrue(environment_is_ready("none", 1, None))
        self.assertEqual(environment_status("none", 1, None), "not_required")

    def test_only_current_verified_revision_is_ready(self) -> None:
        self.assertTrue(environment_is_ready("dockerfile", 4, 4))
        self.assertFalse(environment_is_ready("dockerfile", 4, 3))
        self.assertFalse(environment_is_ready("image", 4, None))
        self.assertEqual(environment_status("dockerfile", 4, 3), "verification_required")

    def test_runtime_requires_a_verified_image_identity_for_containers(self) -> None:
        self.assertTrue(
            environment_is_verified_for_runtime("dockerfile", 4, 4, "sha256:abc")
        )
        self.assertFalse(
            environment_is_verified_for_runtime("dockerfile", 4, 4, None)
        )
        self.assertTrue(environment_is_verified_for_runtime("none", 1, None, None))

    def test_legacy_unverified_is_explicit(self) -> None:
        self.assertEqual(environment_status("image", 1, None), "legacy_unverified")
        self.assertEqual(environment_status("image", 2, None), "verification_required")

    def test_only_runtime_contract_fields_invalidate_verification(self) -> None:
        current = {
            "env_type": "dockerfile",
            "dockerfile_content": "FROM python",
            "docker_image": "ctf-challenge-1:latest",
            "container_port": 5000,
        }
        self.assertFalse(environment_configuration_changed(current, {"name": "new title"}))
        self.assertTrue(environment_configuration_changed(current, {"container_port": 8080}))
        self.assertTrue(environment_configuration_changed(current, {"docker_image": "other:latest"}))

    def test_static_and_regex_policy_changes_invalidate_container_verification(self) -> None:
        current_flags = [
            {"type": "regex", "value": r"^CTF\\{[a-z]+\\}$", "case_sensitive": True}
        ]
        self.assertFalse(
            flag_policy_configuration_changed(
                current_type="regex",
                current_value="",
                current_flags=current_flags,
                proposed_type="regex",
                proposed_value="",
                proposed_flags=current_flags,
            )
        )
        self.assertTrue(
            flag_policy_configuration_changed(
                current_type="regex",
                current_value="",
                current_flags=current_flags,
                proposed_type="regex",
                proposed_value="",
                proposed_flags=[
                    {"type": "regex", "value": r"^FLAG\\{[a-z]+\\}$", "case_sensitive": True}
                ],
            )
        )
        self.assertTrue(
            flag_policy_configuration_changed(
                current_type="static",
                current_value="old-value",
                current_flags=[],
                proposed_type="static",
                proposed_value="new-value",
                proposed_flags=[],
            )
        )

    def test_revision_never_moves_backwards(self) -> None:
        self.assertEqual(next_environment_revision(4), 5)
        self.assertEqual(next_environment_revision(None), 1)
        self.assertEqual(next_environment_revision(0), 1)

    def test_failed_verification_codes_are_normalized_without_downstream_text(self) -> None:
        self.assertEqual(
            normalize_environment_failure_code("port_unreachable"),
            "port_unreachable",
        )
        self.assertEqual(
            normalize_environment_failure_code("untrusted downstream body"),
            "verification_execution_failed",
        )


if __name__ == "__main__":
    unittest.main()
