"""Static regression checks for the admin Docker build retry contract.

This test reads the source only. It does not upload files, build an image, or
contact Learn/Env-orchestrator.
"""

from pathlib import Path
import re
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class GenericDockerBuildUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            PROJECT_ROOT
            / "frontend"
            / "src"
            / "components"
            / "admin"
            / "challenges"
            / "StepEnvironment.tsx"
        ).read_text(encoding="utf-8")

    def callback(self, name: str) -> str:
        match = re.search(
            rf"const\s+{name}\s*=\s*useCallback\((.*?)^  \}},\s*\[",
            self.source, re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(match, f"{name} callback이 있어야 합니다.")
        return match.group(1)

    def test_auto_attempt_marker_does_not_gate_manual_retry(self) -> None:
        build = self.callback("handleBuild")
        self.assertNotIn("hasAutoBuildAttemptedRef", build)
        self.assertRegex(self.source, r"if\s*\(hasAutoBuildAttemptedRef\.current\)\s*return")
        self.assertRegex(self.source, r"hasAutoBuildAttemptedRef\.current\s*=\s*true;\s*void\s+handleBuild\(\)")
        self.assertIn("hasAutoBuildAttemptedRef.current = false", self.source)
        self.assertIn("onClick={handleBuild}", self.source)

    def test_build_and_verify_share_an_operation_gate_and_target_guard(self) -> None:
        gate = self.callback("beginEnvironmentOperation")
        self.assertRegex(gate, r"if\s*\(activeEnvironmentOperationRef\.current\s*!==\s*null\)\s*return null")
        for name in ("handleBuild", "handleVerifyEnvironment"):
            with self.subTest(callback=name):
                body = self.callback(name)
                self.assertIn("beginEnvironmentOperation(targetChallengeId)", body)
                self.assertIn("if (!operation)", body)
                self.assertIn("isCurrentEnvironmentResult(generation, targetChallengeId)", body)
                self.assertIn("endEnvironmentOperation(operation)", body)

    def test_finally_cleans_only_the_owned_timer_and_busy_state(self) -> None:
        build = self.callback("handleBuild")
        finally_body = build.rsplit("finally", 1)[1]
        self.assertIn("endEnvironmentOperation(operation)", finally_body)
        self.assertIn("setBuilding(false)", finally_body)
        self.assertIn("clearInterval(operationTimer)", finally_body)
        self.assertIn("buildTimerRef.current === operationTimer", finally_body)
        self.assertIn("buildTimerRef.current = null", finally_body)


if __name__ == "__main__":
    unittest.main()
