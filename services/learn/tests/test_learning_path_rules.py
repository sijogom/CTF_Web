"""Focused contracts for the authoritative learning-path V1 slice.

These tests deliberately avoid a database or Docker runtime.  PostgreSQL
transaction behaviour and end-to-end visibility checks remain deployment
validation work; here we lock the pure response and source-level boundaries.
"""
from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest

from pydantic import ValidationError


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.schemas.challenge import (  # noqa: E402
    ChallengeCreate,
    ChallengeUpdate,
    LearningPathChallengeResponse,
)
from app.utils.learning_path import (  # noqa: E402
    build_challenge_learning_path_projection,
    system_prerequisite_edge_id,
)


def _target() -> dict:
    return {
        "type": "challenge",
        "id": "42",
        "name": "SQL Intro",
        "href": "/wargame/42",
    }


def _step(*, status: str, href: str = "/learn/course-a") -> dict:
    return {
        "type": "course",
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "SQL Basics",
        "status": status,
        "href": href,
        "graph_focus": {
            "focus_type": "course",
            "focus_id": "11111111-1111-1111-1111-111111111111",
        },
        "completed_units": 1,
        "total_units": 4,
    }


class LearningPathProjectionTests(unittest.TestCase):
    def test_projection_is_public_safe_and_tells_anonymous_user_to_sign_in(self) -> None:
        response = build_challenge_learning_path_projection(
            target=_target(),
            is_authenticated=False,
            required_steps=[_step(status="unknown")],
            recommended_steps=[],
            helpful_content=[],
        )
        parsed = LearningPathChallengeResponse.model_validate(response)
        self.assertEqual(parsed.access_state, "sign_in_required")
        self.assertEqual(parsed.reason_code, "authentication_required")
        self.assertEqual(parsed.next_action.kind, "sign_in")
        self.assertEqual(parsed.next_action.href, "/login?next=/wargame/42")
        self.assertEqual(
            parsed.map_focus_href,
            "/learn?view=map&focus_type=challenge&focus_id=42",
        )

    def test_projection_selects_the_first_incomplete_step_for_a_signed_in_user(self) -> None:
        response = build_challenge_learning_path_projection(
            target=_target(),
            is_authenticated=True,
            required_steps=[_step(status="completed"), _step(status="in_progress", href="/learn/course-b")],
            recommended_steps=[],
            helpful_content=[],
        )
        parsed = LearningPathChallengeResponse.model_validate(response)
        self.assertEqual(parsed.access_state, "locked")
        self.assertEqual(parsed.reason_code, "required_steps_incomplete")
        self.assertEqual(parsed.next_action.kind, "open_content")
        self.assertEqual(parsed.next_action.href, "/learn/course-b")

    def test_projection_fails_closed_for_an_unavailable_legacy_requirement(self) -> None:
        response = build_challenge_learning_path_projection(
            target=_target(),
            is_authenticated=True,
            required_steps=[
                {
                    "type": "challenge",
                    "id": "unavailable-1",
                    "name": "사용할 수 없는 선행 콘텐츠",
                    "status": "unavailable",
                    "href": "/inquiry",
                    "graph_focus": {"focus_type": "challenge", "focus_id": "42"},
                }
            ],
            recommended_steps=[],
            helpful_content=[],
        )
        parsed = LearningPathChallengeResponse.model_validate(response)
        self.assertEqual(parsed.access_state, "locked")
        self.assertEqual(parsed.reason_code, "required_content_unavailable")
        self.assertEqual(parsed.next_action.kind, "contact_admin")

    def test_derived_system_edge_id_is_stable_and_directional(self) -> None:
        forward = system_prerequisite_edge_id(
            source_type="course",
            source_id="11111111-1111-1111-1111-111111111111",
            target_type="challenge",
            target_id="42",
        )
        same_forward = system_prerequisite_edge_id(
            source_type="course",
            source_id="11111111-1111-1111-1111-111111111111",
            target_type="challenge",
            target_id="42",
        )
        reverse = system_prerequisite_edge_id(
            source_type="challenge",
            source_id="42",
            target_type="course",
            target_id="11111111-1111-1111-1111-111111111111",
        )
        self.assertEqual(forward, same_forward)
        self.assertNotEqual(forward, reverse)


class LearningPathInputContractTests(unittest.TestCase):
    def test_schema_rejects_non_positive_and_duplicate_challenge_prerequisites(self) -> None:
        with self.assertRaises(ValidationError):
            ChallengeCreate(
                name="duplicate prerequisite",
                flag_value="FLAG{safe}",
                prerequisite_ids=[7, 7],
            )
        with self.assertRaises(ValidationError):
            ChallengeUpdate(prerequisite_ids=[0])

    def test_helpful_content_has_an_explicit_public_contract(self) -> None:
        response = LearningPathChallengeResponse.model_validate(
            {
                "target": _target(),
                "access_state": "available",
                "reason_code": "all_required_steps_completed",
                "required_steps": [],
                "recommended_steps": [],
                "helpful_content": [
                    {
                        "type": "lesson",
                        "id": "11111111-1111-1111-1111-111111111111",
                        "name": "SQL 입력 검증",
                        "status": "unknown",
                        "href": "/learn/course/lesson",
                        "graph_focus": {
                            "focus_type": "lesson",
                            "focus_id": "11111111-1111-1111-1111-111111111111",
                        },
                        "relation_type": "helps",
                        "strength": 0.8,
                        "confidence": 0.7,
                        "evidence_summary": "관리자 승인 관계",
                        "origin": "manual",
                    }
                ],
                "next_action": {"kind": "open_challenge", "href": "/wargame/42"},
                "map_focus_href": "/learn?view=map&focus_type=challenge&focus_id=42",
            }
        )
        self.assertEqual(response.helpful_content[0].relation_type, "helps")


class LearningPathSourceContractTests(unittest.TestCase):
    def test_endpoint_validation_and_read_only_system_edge_contract_are_present(self) -> None:
        challenge_source = (SERVICE_ROOT / "app" / "api" / "challenges.py").read_text(encoding="utf-8")
        graph_source = (SERVICE_ROOT / "app" / "api" / "knowledge_graph.py").read_text(encoding="utf-8")
        schema_source = (SERVICE_ROOT / "app" / "schemas" / "challenge.py").read_text(encoding="utf-8")
        utility_source = (SERVICE_ROOT / "app" / "utils" / "learning_path.py").read_text(encoding="utf-8")

        self.assertIn('"/learning-path/challenges/{challenge_id}"', challenge_source)
        self.assertIn("_validate_challenge_prerequisites", challenge_source)
        self.assertIn("require_completable_course=True", challenge_source)
        self.assertIn("선행 문제 조건에 순환 참조를 만들 수 없습니다.", challenge_source)
        self.assertIn("class LearningPathHelpfulContent", schema_source)
        self.assertIn("system_prerequisite_edge_id", graph_source)
        self.assertIn('origin="system_prerequisite"', graph_source)
        self.assertIn("_v2_system_prerequisite_edges", graph_source)
        self.assertIn("never use ``KnowledgeRelation``", graph_source)
        graph_tree = ast.parse(graph_source)
        graph_functions = {
            node.name: ast.get_source_segment(graph_source, node) or ""
            for node in ast.walk(graph_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertNotIn(
            "dict(graph.layout_hints)",
            graph_functions["_v2_public_relation_edges"],
        )
        self.assertIn(
            "dict(graph.layout_hints)",
            graph_functions["_v2_system_prerequisite_edges"],
        )
        self.assertNotIn("from app.models.knowledge", utility_source)
        self.assertNotIn("db.add(", utility_source)

        challenge_tree = ast.parse(challenge_source)
        challenge_functions = {
            node.name: ast.get_source_segment(challenge_source, node) or ""
            for node in ast.walk(challenge_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        path_builder = challenge_functions["_build_challenge_learning_path"]
        self.assertNotIn("_unavailable_learning_path_step(", path_builder)
        self.assertGreaterEqual(path_builder.count("if step is not None:"), 2)
        list_source = challenge_functions["list_challenges"]
        self.assertIn(
            'if not is_admin:\n        query = query.where(Challenge.challenge_type == "wargame")',
            list_source,
        )
        self.assertIn(
            "_validate_competition_mandatory_prerequisites",
            challenge_source,
        )
        submit_source = challenge_functions["submit_flag"]
        self.assertIn("if not is_competition:\n        prerequisite_query", submit_source)
        self.assertIn("if not is_competition:\n        # 학습 선행조건 검증", submit_source)


if __name__ == "__main__":
    unittest.main()
