from __future__ import annotations

import asyncio
import ast
from pathlib import Path
import sys
import unittest
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.api.challenges import _get_challenge_tags, _get_recommended_prerequisites
from app.api.knowledge_tags import list_unmapped_content, reconcile_rule_mappings
from app.utils.knowledge_candidate_generation import (
    _challenge_tag_map,
    _course_tag_map,
    _lesson_tag_map,
)

class _AsyncConnectionAdapter:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def execute(self, statement):
        return self.connection.execute(statement)


class _Rows:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _SequencedSession:
    def __init__(self, rows_by_call) -> None:
        self._rows_by_call = iter(rows_by_call)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows(next(self._rows_by_call))


def _function_source(relative_path: str, function_name: str) -> str:
    source = (SERVICE_ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            segment = ast.get_source_segment(source, node)
            if segment is None:
                break
            return segment
    raise AssertionError(f"function not found: {relative_path}:{function_name}")


class PublicTagMappingApprovalBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        self.connection = self.engine.connect()
        for statement in (
            "CREATE TABLE knowledge_tags (id INTEGER PRIMARY KEY, name TEXT, slug TEXT, parent_id INTEGER)",
            "CREATE TABLE challenges (id INTEGER PRIMARY KEY, name TEXT, is_published BOOLEAN, challenge_type TEXT)",
            "CREATE TABLE courses (id UUID PRIMARY KEY, title TEXT, category TEXT, is_deleted BOOLEAN, is_published BOOLEAN, status TEXT)",
            "CREATE TABLE lessons (id UUID PRIMARY KEY, title TEXT, course_id UUID)",
            "CREATE TABLE challenge_knowledge_tags (challenge_id INTEGER, tag_id INTEGER, origin TEXT, approved_at DATETIME)",
            "CREATE TABLE course_knowledge_tags (course_id UUID, tag_id INTEGER, origin TEXT, approved_at DATETIME)",
            "CREATE TABLE lesson_knowledge_tags (lesson_id UUID, tag_id INTEGER, origin TEXT, approved_at DATETIME)",
            "CREATE TABLE challenge_recommended_prerequisites (id INTEGER PRIMARY KEY, challenge_id INTEGER, rec_type TEXT, rec_target_id TEXT, created_at DATETIME)",
            "CREATE TABLE challenge_learning_prerequisites (id INTEGER PRIMARY KEY, challenge_id INTEGER, prerequisite_type TEXT, prerequisite_target_id UUID)",
        ):
            self.connection.exec_driver_sql(statement)

        self.pending_course_id = UUID("a0000000-0000-0000-0000-000000000001")
        self.approved_course_id = UUID("b0000000-0000-0000-0000-000000000002")
        self.pending_lesson_id = UUID("c0000000-0000-0000-0000-000000000001")
        self.approved_lesson_id = UUID("d0000000-0000-0000-0000-000000000002")
        self.connection.execute(
            text("INSERT INTO knowledge_tags (id, name, slug) VALUES (1, 'Shared', 'shared')")
        )
        self.connection.execute(
            text(
                "INSERT INTO challenges (id, name, is_published, challenge_type) VALUES "
                "(1, 'Publisher A', 1, 'wargame'), "
                "(2, 'Pending B', 1, 'wargame'), "
                "(3, 'Approved B', 1, 'wargame'), "
                "(4, 'Legacy B', 1, 'wargame')"
            )
        )
        self.connection.execute(
            text(
                "INSERT INTO challenge_knowledge_tags (challenge_id, tag_id, origin, approved_at) VALUES "
                "(1, 1, 'manual', NULL), "
                "(2, 1, 'model_candidate', NULL), "
                "(3, 1, 'model_candidate', '2026-09-08 00:00:00'), "
                "(4, 1, NULL, NULL)"
            )
        )
        self.connection.execute(
            text(
                "INSERT INTO courses (id, title, category, is_deleted, is_published, status) VALUES "
                "(:pending, 'Pending course', 'misc', 0, 1, 'approved'), "
                "(:approved, 'Approved course', 'misc', 0, 1, 'approved')"
            ),
            {
                "pending": self.pending_course_id.hex,
                "approved": self.approved_course_id.hex,
            },
        )
        self.connection.execute(
            text(
                "INSERT INTO course_knowledge_tags (course_id, tag_id, origin, approved_at) VALUES "
                "(:pending, 1, 'model_candidate', NULL), "
                "(:approved, 1, 'model_candidate', '2026-09-08 00:00:00')"
            ),
            {
                "pending": self.pending_course_id.hex,
                "approved": self.approved_course_id.hex,
            },
        )
        self.connection.execute(
            text(
                "INSERT INTO lessons (id, title, course_id) VALUES "
                "(:pending_lesson, 'Pending lesson', :pending_course), "
                "(:approved_lesson, 'Approved lesson', :approved_course)"
            ),
            {
                "pending_lesson": self.pending_lesson_id.hex,
                "pending_course": self.pending_course_id.hex,
                "approved_lesson": self.approved_lesson_id.hex,
                "approved_course": self.approved_course_id.hex,
            },
        )
        self.connection.execute(
            text(
                "INSERT INTO lesson_knowledge_tags (lesson_id, tag_id, origin, approved_at) VALUES "
                "(:pending, 1, 'model_candidate', NULL), "
                "(:approved, 1, 'model_candidate', '2026-09-08 00:00:00')"
            ),
            {
                "pending": self.pending_lesson_id.hex,
                "approved": self.approved_lesson_id.hex,
            },
        )
        self.connection.commit()
        self.db = _AsyncConnectionAdapter(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.engine.dispose()

    def test_candidate_inputs_exclude_only_the_pending_junction_for_a_public_tag(self) -> None:
        async def exercise() -> None:
            self.assertEqual(await _challenge_tag_map(self.db, [2]), {})
            self.assertEqual(await _challenge_tag_map(self.db, [3]), {3: {"shared"}})
            self.assertEqual(await _challenge_tag_map(self.db, [4]), {4: {"shared"}})
            self.assertEqual(await _course_tag_map(self.db, [self.pending_course_id]), {})
            self.assertEqual(
                await _course_tag_map(self.db, [self.approved_course_id]),
                {self.approved_course_id: {"shared"}},
            )
            self.assertEqual(await _lesson_tag_map(self.db, [self.pending_lesson_id]), {})
            self.assertEqual(
                await _lesson_tag_map(self.db, [self.approved_lesson_id]),
                {self.approved_lesson_id: {"shared"}},
            )

            self.connection.execute(
                text(
                    "UPDATE challenge_knowledge_tags SET approved_at = :approved "
                    "WHERE challenge_id = 2"
                ),
                {"approved": "2026-09-08 00:00:00"},
            )
            self.connection.execute(
                text(
                    "UPDATE course_knowledge_tags SET approved_at = :approved "
                    "WHERE course_id = :course_id"
                ),
                {
                    "approved": "2026-09-08 00:00:00",
                    "course_id": self.pending_course_id.hex,
                },
            )
            self.connection.execute(
                text(
                    "UPDATE lesson_knowledge_tags SET approved_at = :approved "
                    "WHERE lesson_id = :lesson_id"
                ),
                {
                    "approved": "2026-09-08 00:00:00",
                    "lesson_id": self.pending_lesson_id.hex,
                },
            )
            self.assertEqual(await _challenge_tag_map(self.db, [2]), {2: {"shared"}})
            self.assertEqual(
                await _course_tag_map(self.db, [self.pending_course_id]),
                {self.pending_course_id: {"shared"}},
            )
            self.assertEqual(
                await _lesson_tag_map(self.db, [self.pending_lesson_id]),
                {self.pending_lesson_id: {"shared"}},
            )

        asyncio.run(exercise())

    def test_list_detail_and_recommendations_share_the_junction_boundary(self) -> None:
        async def exercise() -> None:
            self.assertEqual(await _get_challenge_tags([2], self.db), {})
            self.assertEqual(await _get_challenge_tags([3], self.db), {3: ["shared"]})
            self.assertEqual(await _get_recommended_prerequisites(2, self.db), [])

            self.connection.execute(
                text(
                    "UPDATE challenge_knowledge_tags SET approved_at = '2026-09-08 00:00:00' "
                    "WHERE challenge_id = 2"
                )
            )
            recommendations = await _get_recommended_prerequisites(2, self.db)
            self.assertEqual(
                recommendations,
                [
                    {
                        "type": "course",
                        "id": str(self.approved_course_id),
                        "name": "Approved course",
                    }
                ],
            )

            self.connection.execute(
                text(
                    "UPDATE course_knowledge_tags SET approved_at = NULL "
                    "WHERE course_id = :course_id"
                ),
                {"course_id": self.approved_course_id.hex},
            )
            self.assertEqual(await _get_recommended_prerequisites(2, self.db), [])

        asyncio.run(exercise())

    def test_pending_only_junctions_remain_visible_in_the_unmapped_queue(self) -> None:
        async def exercise() -> None:
            items = await list_unmapped_content(
                entity_type="all",
                limit=50,
                db=self.db,
                _="admin",
            )
            self.assertEqual(
                {(item.entity_type, item.entity_id) for item in items},
                {
                    ("challenge", "2"),
                    ("course", str(self.pending_course_id)),
                    ("lesson", str(self.pending_lesson_id)),
                },
            )

            self.connection.execute(
                text(
                    "UPDATE challenge_knowledge_tags SET approved_at = '2026-09-08 00:00:00' "
                    "WHERE challenge_id = 2"
                )
            )
            self.connection.execute(
                text(
                    "UPDATE course_knowledge_tags SET approved_at = '2026-09-08 00:00:00' "
                    "WHERE course_id = :course_id"
                ),
                {"course_id": self.pending_course_id.hex},
            )
            self.connection.execute(
                text(
                    "UPDATE lesson_knowledge_tags SET approved_at = '2026-09-08 00:00:00' "
                    "WHERE lesson_id = :lesson_id"
                ),
                {"lesson_id": self.pending_lesson_id.hex},
            )
            self.assertEqual(
                await list_unmapped_content(
                    entity_type="all",
                    limit=50,
                    db=self.db,
                    _="admin",
                ),
                [],
            )

        asyncio.run(exercise())

    def test_reconcile_does_not_launder_a_pending_course_mapping_into_a_lesson_rule(self) -> None:
        course_id = uuid4()
        lesson_id = uuid4()
        pending_tag_id = 2
        session = _SequencedSession(
            [
                [(1, "web"), (pending_tag_id, "pending")],
                [],
                [(course_id, "web")],
                [(lesson_id, course_id)],
                [],
                [(course_id, pending_tag_id)],
                [],
                [],
            ]
        )

        result = asyncio.run(
            reconcile_rule_mappings(
                dry_run=True,
                db=session,
                actor_id=uuid4(),
                _role="admin",
            )
        )

        self.assertEqual(result.candidates, 2)
        self.assertEqual(result.by_entity, {"challenge": 0, "course": 1, "lesson": 1})
        self.assertEqual(result.created, 0)
        self.assertEqual(result.existing, 0)
        self.assertEqual(len(session.statements), 8)
        authoritative_sql = str(session.statements[6])
        self.assertIn("course_knowledge_tags.origin", authoritative_sql)
        self.assertIn("course_knowledge_tags.approved_at", authoritative_sql)

    def test_every_public_consumer_calls_the_shared_junction_clause(self) -> None:
        candidate_source = "\n".join(
            _function_source("app/utils/knowledge_candidate_generation.py", function_name)
            for function_name in ("_challenge_tag_map", "_course_tag_map", "_lesson_tag_map")
        )
        for model_name in (
            "ChallengeKnowledgeTag",
            "CourseKnowledgeTag",
            "LessonKnowledgeTag",
        ):
            self.assertIn(f"public_tag_mapping_clause({model_name})", candidate_source)

        challenge_tag_source = _function_source("app/api/challenges.py", "_get_challenge_tags")
        recommendation_source = _function_source(
            "app/api/challenges.py", "_get_recommended_prerequisites"
        )
        challenge_detail_source = _function_source("app/api/challenges.py", "get_challenge")
        self.assertIn(
            "public_tag_mapping_clause(ChallengeKnowledgeTag)", challenge_tag_source
        )
        self.assertIn(
            "public_tag_mapping_clause(ChallengeKnowledgeTag)", recommendation_source
        )
        self.assertIn(
            "public_tag_mapping_clause(CourseKnowledgeTag)", recommendation_source
        )
        self.assertIn(
            "public_tag_mapping_clause(ChallengeKnowledgeTag)", challenge_detail_source
        )

        graph_source = _function_source("app/api/knowledge_graph.py", "_build_graph")
        for model_name in (
            "ChallengeKnowledgeTag",
            "CourseKnowledgeTag",
            "LessonKnowledgeTag",
        ):
            self.assertIn(f"public_tag_mapping_clause({model_name})", graph_source)

        unmapped_source = _function_source("app/api/knowledge_tags.py", "list_unmapped_content")
        for model_name in (
            "ChallengeKnowledgeTag",
            "CourseKnowledgeTag",
            "LessonKnowledgeTag",
        ):
            self.assertIn(f"public_tag_mapping_clause({model_name})", unmapped_source)


if __name__ == "__main__":
    unittest.main()
