"""CTF 정답 제출 시각과 Auth 점수 반영 계약의 정적 회귀 테스트."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
LEARN_CHALLENGES = SERVICE_ROOT / "app" / "api" / "challenges.py"
AUTH_INTERNAL = SERVICE_ROOT.parents[1] / "services" / "auth" / "app" / "api" / "internal.py"
AUTH_ACCESS = (
    SERVICE_ROOT.parents[1]
    / "services"
    / "auth"
    / "app"
    / "utils"
    / "competition_access.py"
)


def _function(tree: ast.AST, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function not found: {name}")


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


class CtfScoreAcceptanceContractTests(unittest.TestCase):
    def test_learn_records_the_original_submission_time(self) -> None:
        source = LEARN_CHALLENGES.read_text(encoding="utf-8")
        tree = ast.parse(source)
        recorder = _function(tree, "_record_competition_solve")

        accepted_at_arg = {
            argument.arg
            for argument in (*recorder.args.args, *recorder.args.kwonlyargs)
            if argument.arg == "accepted_at"
        }
        self.assertEqual(accepted_at_arg, {"accepted_at"})

        payload_keys = {
            key.value
            for node in ast.walk(recorder)
            if isinstance(node, ast.Dict)
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        self.assertIn("accepted_at", payload_keys)

        solve_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_record_competition_solve"
        ]
        self.assertGreaterEqual(len(solve_calls), 3)
        for call in solve_calls:
            accepted_at = _keyword(call, "accepted_at")
            self.assertIsNotNone(accepted_at)
            assert accepted_at is not None
            self.assertIsInstance(accepted_at, ast.Attribute)
            self.assertEqual(accepted_at.attr, "submitted_at")

    def test_auth_uses_accepted_time_for_period_and_solved_at(self) -> None:
        access_tree = ast.parse(AUTH_ACCESS.read_text(encoding="utf-8"))
        access_function = _function(access_tree, "require_competition_challenge_access")
        args = {
            argument.arg
            for argument in (*access_function.args.args, *access_function.args.kwonlyargs)
            if argument.arg == "accepted_at"
        }
        self.assertEqual(args, {"accepted_at"})

        access_source = AUTH_ACCESS.read_text(encoding="utf-8")
        self.assertIn("access_time = accepted_at.astimezone(timezone.utc)", access_source)
        self.assertIn("access_time >= competition.end_at", access_source)

        internal_source = AUTH_INTERNAL.read_text(encoding="utf-8")
        self.assertIn("class CompetitionSolveRequest", internal_source)
        self.assertIn("accepted_at: datetime", internal_source)
        self.assertIn("solved_at=body.accepted_at", internal_source)


if __name__ == "__main__":
    unittest.main()
