"""Focused regression tests for learning-path and CTF visibility repairs.

These tests use in-memory fakes only.  They do not start Docker, call Auth,
or require a database server.
"""
from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

try:
    from fastapi import HTTPException
    from app.api import challenges, knowledge_graph
    from app.schemas.knowledge import KnowledgeMapV2FocusResponse
    from app.utils.knowledge_relations import ResolvedKnowledgeContent
except ModuleNotFoundError as exc:  # pragma: no cover - environment boundary
    if exc.name != "sqlalchemy":
        raise
    HTTPException = None
    challenges = None
    knowledge_graph = None
    KnowledgeMapV2FocusResponse = None
    ResolvedKnowledgeContent = None
    RUNTIME_IMPORT_ERROR = "sqlalchemy is not installed in this local Python"
else:
    RUNTIME_IMPORT_ERROR = None


class _ScalarRows:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[object]:
        return self._values


class _QueryResult:
    def __init__(self, *, rows: list[object] | None = None, scalars: list[object] | None = None) -> None:
        self._rows = rows or []
        self._scalars = scalars or []

    def all(self) -> list[object]:
        return self._rows

    def scalars(self) -> _ScalarRows:
        return _ScalarRows(self._scalars)


class _SystemPrerequisiteDB:
    """Only the two read queries used by the focused system-edge path."""

    def __init__(self) -> None:
        self._query_count = 0

    async def execute(self, _statement):  # noqa: ANN001
        self._query_count += 1
        if self._query_count == 1:
            # (target challenge, source prerequisite)
            return _QueryResult(rows=[(2, 1)])
        if self._query_count == 2:
            return _QueryResult(scalars=[])
        raise AssertionError("unexpected database query in system prerequisite edge test")


class SourceContractTests(unittest.TestCase):
    def test_public_list_and_generic_detail_keep_ctf_metadata_outside_auth_context(self) -> None:
        source = (SERVICE_ROOT / "app" / "api" / "challenges.py").read_text(encoding="utf-8")
        self.assertIn(
            'if not is_admin:\n        query = query.where(Challenge.challenge_type == "wargame")',
            source,
        )
        self.assertIn('elif challenge_type in ("wargame", "competition"):', source)
        self.assertIn('if challenge.challenge_type == "competition" and not is_admin:', source)
        self.assertIn('"/competitions/{competition_id}/challenges/{challenge_id}"', source)

    def test_legacy_ctf_rows_are_not_rewritten_by_unrelated_updates(self) -> None:
        source = (SERVICE_ROOT / "app" / "api" / "challenges.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        update_handler = next(
            ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "update_challenge"
        )
        self.assertIn("if challenge.challenge_type != target_challenge_type:", update_handler)
        self.assertIn("if not has_new_prereqs:", update_handler)
        self.assertIn("if not has_new_learning_prereqs:", update_handler)
        self.assertIn("explicitly clear them with empty replacement lists", update_handler)

    def test_legacy_policy_is_shared_by_list_detail_submit_and_learning_path(self) -> None:
        source = (SERVICE_ROOT / "app" / "api" / "challenges.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        helper = functions["_legacy_learning_prerequisite_is_enforceable"]
        self.assertIn('prerequisite_type == "course"', helper)
        self.assertIn("course_has_lessons is True", helper)
        self.assertIn("_legacy_learning_prerequisite_is_enforceable", functions["_check_learning_prerequisites"])
        self.assertIn("_legacy_learning_prerequisite_is_enforceable", functions["list_challenges"])
        self.assertIn("_legacy_learning_prerequisite_is_enforceable", functions["_build_challenge_learning_path"])

    def test_v2_requires_path_initializes_layout_hints_before_system_edges(self) -> None:
        source = (SERVICE_ROOT / "app" / "api" / "knowledge_graph.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_v2_system_prerequisite_edges"
        )
        body = ast.get_source_segment(source, function) or ""
        self.assertIn("layout_hints = dict(graph.layout_hints)", body)
        self.assertLess(
            body.index("layout_hints = dict(graph.layout_hints)"),
            body.index("system_edges: list[KnowledgeMapV2Edge]"),
        )
        self.assertIn('relation_type="requires"', body)
        self.assertIn('origin="system_prerequisite"', body)


@unittest.skipIf(RUNTIME_IMPORT_ERROR is not None, RUNTIME_IMPORT_ERROR)
class LearningPathLegacyPolicyTests(unittest.TestCase):
    def test_legacy_unavailable_learning_rows_are_never_enforced(self) -> None:
        policy = challenges._legacy_learning_prerequisite_is_enforceable
        self.assertFalse(
            policy("course", target_is_public=False, course_has_lessons=True)
        )
        self.assertFalse(
            policy("course", target_is_public=True, course_has_lessons=False)
        )
        self.assertTrue(
            policy("course", target_is_public=True, course_has_lessons=True)
        )
        self.assertFalse(policy("lesson", target_is_public=False))
        self.assertTrue(policy("lesson", target_is_public=True))
        self.assertFalse(policy("unknown", target_is_public=True))

    def test_new_ctf_mandatory_prerequisites_are_rejected_but_empty_policy_is_allowed(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            challenges._validate_competition_mandatory_prerequisites(
                challenge_type="competition",
                prerequisite_ids=[10],
                learning_prerequisites=[],
            )
        self.assertEqual(raised.exception.status_code, 422)

        challenges._validate_competition_mandatory_prerequisites(
            challenge_type="competition",
            prerequisite_ids=[],
            learning_prerequisites=[],
        )

@unittest.skipIf(RUNTIME_IMPORT_ERROR is not None, RUNTIME_IMPORT_ERROR)
class KnowledgeMapSystemPrerequisiteFocusTests(unittest.IsolatedAsyncioTestCase):
    async def test_focus_requires_path_preserves_layout_hints_and_adds_system_edge(self) -> None:
        source = ResolvedKnowledgeContent(
            entity_type="challenge",
            entity_id="1",
            display_name="Prerequisite",
            is_active=True,
            href="/wargame/1",
        )
        target = ResolvedKnowledgeContent(
            entity_type="challenge",
            entity_id="2",
            display_name="Target",
            is_active=True,
            href="/wargame/2",
        )
        focus_node = knowledge_graph._v2_node(target, distance=0, is_focus=True)
        graph = KnowledgeMapV2FocusResponse(
            graph_revision="focused-test",
            focus=focus_node,
            nodes=[focus_node],
            edges=[],
            layout_hints={"strategy": "seeded_force_v1", "distance_by_node": {"challenge:2": 0}},
            applied_filters={"types": ["challenge"], "relation_types": ["requires"], "depth": 1},
        )

        async def resolve_public_content(_db, entity_type: str, entity_id: str, *, public_only: bool, viewer_id=None):  # noqa: ANN001
            self.assertTrue(public_only)
            self.assertIsNone(viewer_id)
            return {
                ("challenge", "1"): source,
                ("challenge", "2"): target,
            }.get((entity_type, entity_id))

        with patch.object(knowledge_graph, "resolve_content", resolve_public_content):
            result = await knowledge_graph._v2_system_prerequisite_edges(
                _SystemPrerequisiteDB(),
                graph=graph,
                focus_content=target,
                depth=1,
                content_types={"challenge"},
                relation_types={"requires"},
            )

        self.assertEqual(len(result.edges), 1)
        self.assertEqual(result.edges[0].relation_type, "requires")
        self.assertEqual(result.edges[0].origin, "system_prerequisite")
        self.assertEqual(result.edges[0].source, "challenge:1")
        self.assertEqual(result.edges[0].target, "challenge:2")
        self.assertEqual(result.layout_hints["distance_by_node"]["challenge:2"], 0)
        self.assertEqual(result.layout_hints["distance_by_node"]["challenge:1"], 1)


if __name__ == "__main__":
    unittest.main()
