"""관리 삭제/검색의 실제 SQL 트랜잭션 검사. PostgreSQL 잠금 인수는 별도다."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI, HTTPException
import httpx
from sqlalchemy import BigInteger, create_engine, event, select
from sqlalchemy.dialects.postgresql import JSONB, dialect
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

from app.api import deps, knowledge_contents, knowledge_tags
from app.database import get_db
from app.models.challenge import Challenge, Hint
from app.models.course import Base, Course, Lesson
from app.models.education import EducationAssignment, TeachingClass
from app.models.event import PlatformEvent
from app.models.knowledge import (
    ChallengeKnowledgeTag, CourseKnowledgeTag, KnowledgeContentNode, KnowledgeContentProfile,
    KnowledgeCandidateRun, KnowledgeProjectionOutbox, KnowledgeRelation, KnowledgeRelationProposal,
    KnowledgeRelationProposalReview, KnowledgeRelationReview, KnowledgeTag, KnowledgeTagDeletionArchive,
    KnowledgeTagMappingAuditEvent, LessonKnowledgeTag,
)
from app.utils.knowledge_tag_deletion import delete_tag_preserving_history, load_tag_deletion_impact


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(_type, _compiler, **_kwargs):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _sqlite_bigint(_type, _compiler, **_kwargs):
    return "INTEGER"


class AsyncAdapter:
    def __init__(self, session):
        self.session = session
        self.commits = 0

    async def scalar(self, query):
        return self.session.scalar(query)

    async def scalars(self, query):
        return self.session.scalars(query)

    async def execute(self, query):
        return self.session.execute(query)

    async def get(self, model, key):
        return self.session.get(model, key)

    def add(self, row):
        self.session.add(row)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.commits += 1
        self.session.commit()

    async def rollback(self):
        self.session.rollback()


class KnowledgeManagementCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        @event.listens_for(self.engine, "connect")
        def enable_fk(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
        models = (KnowledgeTag, Challenge, Hint, Course, Lesson, ChallengeKnowledgeTag,
            CourseKnowledgeTag, LessonKnowledgeTag, KnowledgeContentNode, KnowledgeContentProfile,
            KnowledgeRelation, KnowledgeRelationReview, KnowledgeCandidateRun, KnowledgeRelationProposal,
            KnowledgeRelationProposalReview, KnowledgeTagMappingAuditEvent, KnowledgeTagDeletionArchive,
            KnowledgeProjectionOutbox, TeachingClass, EducationAssignment, PlatformEvent)
        Base.metadata.create_all(self.engine, tables=[model.__table__ for model in models])
        self.session = Session(self.engine, expire_on_commit=False)
        self.db = AsyncAdapter(self.session)
        self.actor = uuid4()
        self.tag = KnowledgeTag(name="삭제 대상", slug="target", description="원래 설명")
        self.other = KnowledgeTag(name="유지 대상", slug="retained")
        self.challenge = Challenge(name="비공개 문제", description="private body", category="web", difficulty="beginner", is_published=False)
        self.course = Course(title="비공개 코스", category="web", difficulty="basic", author_id=self.actor)
        self.session.add_all([self.tag, self.other, self.challenge, self.course])
        self.session.flush()
        self.child = KnowledgeTag(name="보존 자식", slug="child", parent_id=self.tag.id)
        self.lesson = Lesson(course_id=self.course.id, title="보존 레슨", content="<p>원문</p>", attachments=[])
        self.session.add_all([self.child, self.lesson])
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def seed_graph_and_mappings(self):
        self.tag_node = KnowledgeContentNode(entity_type="knowledge_tag", entity_id=str(self.tag.id), display_name=self.tag.name, is_active=True)
        self.other_node = KnowledgeContentNode(entity_type="challenge", entity_id=str(self.challenge.id), display_name=self.challenge.name, is_active=True)
        self.session.add_all([self.tag_node, self.other_node])
        self.session.flush()
        self.relation = KnowledgeRelation(source_node_id=self.other_node.id, target_node_id=self.tag_node.id,
            relation_type="tag_affinity", created_by=self.actor, status="approved", strength=0.8, confidence=0.9)
        self.pending_relation = KnowledgeRelation(source_node_id=self.tag_node.id, target_node_id=self.other_node.id,
            relation_type="helps", created_by=self.actor, status="pending_review", strength=0.7, confidence=0.8)
        self.session.add_all([self.relation, self.pending_relation])
        self.session.flush()
        self.proposal = KnowledgeRelationProposal(source_node_id=self.other_node.id, target_node_id=self.tag_node.id,
            relation_type="tag_affinity", created_by=self.actor, state="approved", content_revision="a" * 64,
            published_relation_id=self.relation.id, strength=0.8, confidence=0.9)
        self.pending_proposal = KnowledgeRelationProposal(source_node_id=self.tag_node.id, target_node_id=self.other_node.id,
            relation_type="helps", created_by=self.actor, state="pending_review", content_revision="b" * 64,
            strength=0.7, confidence=0.8)
        self.session.add_all([self.proposal, self.pending_proposal])
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.session.add_all([
            ChallengeKnowledgeTag(challenge_id=self.challenge.id, tag_id=self.tag.id, origin="manual", evidence_summary="원본 근거", created_by=self.actor, created_at=now),
            CourseKnowledgeTag(course_id=self.course.id, tag_id=self.tag.id, origin="rule", created_by=self.actor),
            LessonKnowledgeTag(lesson_id=self.lesson.id, tag_id=self.tag.id, origin="import", created_by=self.actor),
            ChallengeKnowledgeTag(challenge_id=self.challenge.id, tag_id=self.other.id, origin="manual", created_by=self.actor),
        ])
        self.old_audit = KnowledgeTagMappingAuditEvent(entity_type="challenge", entity_id=str(self.challenge.id),
            tag_id=self.tag.id, action="attach", actor_id=self.actor, mapping_snapshot={"source": "변경 금지 원문", "nested": [1, 2]}, created_at=now)
        self.session.add(self.old_audit)
        self.session.commit()

    def test_delete_preserves_content_children_old_audit_and_other_tags(self):
        self.seed_graph_and_mappings()
        tag_id = self.tag.id
        before = self.run_async(load_tag_deletion_impact(self.db, tag_id)).preview()
        self.assertEqual(before.counts.model_dump(), {"challenge": 1, "course": 1, "lesson": 1,
            "children": 1, "relations": 2, "proposals": 2, "audit_events": 1})
        self.run_async(knowledge_tags.delete_tag(tag_id, self.db, "admin", self.actor, before.confirmation))
        self.assertEqual(self.db.commits, 1)
        self.assertIsNone(self.session.get(KnowledgeTag, tag_id))
        self.assertIsNotNone(self.session.get(Challenge, self.challenge.id))
        self.assertEqual(self.session.get(Lesson, self.lesson.id).content, "<p>원문</p>")
        self.assertIsNone(self.session.get(KnowledgeTag, self.child.id).parent_id)
        self.assertIsNotNone(self.session.get(KnowledgeTag, self.other.id))
        self.assertIsNotNone(self.session.get(ChallengeKnowledgeTag, (self.challenge.id, self.other.id)))
        for model in (ChallengeKnowledgeTag, CourseKnowledgeTag, LessonKnowledgeTag):
            self.assertEqual(self.session.scalars(select(model).where(model.tag_id == tag_id)).all(), [])
        old = self.session.get(KnowledgeTagMappingAuditEvent, self.old_audit.id)
        self.assertEqual(old.tag_id, tag_id)
        self.assertEqual(old.mapping_snapshot, {"source": "변경 금지 원문", "nested": [1, 2]})
        self.assertEqual(len(self.session.scalars(select(KnowledgeTagMappingAuditEvent)).all()), 4)
        archive = self.session.scalar(select(KnowledgeTagDeletionArchive))
        self.assertEqual(archive.tag_snapshot["name"], "삭제 대상")
        self.assertEqual(archive.actor_id, self.actor)
        self.assertEqual(archive.impact_snapshot["counts"], before.counts.model_dump())
        self.assertFalse(self.session.get(KnowledgeContentNode, self.tag_node.id).is_active)
        self.assertEqual(self.session.get(KnowledgeRelation, self.relation.id).status, "retired")
        self.assertEqual(self.session.get(KnowledgeRelation, self.pending_relation.id).status, "rejected")
        self.assertEqual(self.session.get(KnowledgeRelationProposal, self.proposal.id).state, "retired")
        self.assertEqual(self.session.get(KnowledgeRelationProposal, self.pending_proposal.id).state, "rejected")
        self.assertEqual(len(self.session.scalars(select(KnowledgeRelationReview)).all()), 2)
        self.assertEqual(len(self.session.scalars(select(KnowledgeRelationProposalReview)).all()), 2)
        events = self.session.scalars(select(KnowledgeProjectionOutbox)).all()
        self.assertEqual(len(events), 5)
        self.assertEqual({row.graph_revision for row in events}, {archive.graph_revision})
        self.assertIn("tag_deleted", [row.event_type for row in events])
        history = self.run_async(knowledge_tags.list_mapping_events("challenge", str(self.challenge.id), self.db, "admin"))
        self.assertEqual(len(history), 2)

    def test_delete_without_confirmation_keeps_compatibility_and_second_delete_is_404(self):
        tag_id = self.tag.id
        self.run_async(knowledge_tags.delete_tag(tag_id, self.db, "admin", self.actor, None))
        with self.assertRaises(HTTPException) as raised:
            self.run_async(knowledge_tags.delete_tag(tag_id, self.db, "admin", self.actor, None))
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(len(self.session.scalars(select(KnowledgeTagDeletionArchive)).all()), 1)

    def test_changed_tag_or_same_count_mapping_invalidates_confirmation(self):
        self.seed_graph_and_mappings()
        token = self.run_async(load_tag_deletion_impact(self.db, self.tag.id)).confirmation
        self.tag.name = "다른 이름"
        self.session.commit()
        with self.assertRaises(HTTPException) as raised:
            self.run_async(knowledge_tags.delete_tag(self.tag.id, self.db, "admin", self.actor, token))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.session.scalars(select(KnowledgeTagDeletionArchive)).all(), [])
        token = self.run_async(load_tag_deletion_impact(self.db, self.tag.id)).confirmation
        row = self.session.get(ChallengeKnowledgeTag, (self.challenge.id, self.tag.id))
        row.evidence_summary = "동일 개수지만 근거 변경"
        self.session.commit()
        self.assertNotEqual(token, self.run_async(load_tag_deletion_impact(self.db, self.tag.id)).confirmation)

    def test_failure_after_detach_rolls_back_archive_reviews_mappings_and_tag(self):
        self.seed_graph_and_mappings()
        tag_id = self.tag.id
        with patch.object(self.db, "flush", new=AsyncMock(side_effect=RuntimeError("test failure"))):
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                self.run_async(knowledge_tags.delete_tag(tag_id, self.db, "admin", self.actor, None))
        self.assertIsNotNone(self.session.get(KnowledgeTag, tag_id))
        self.assertIsNotNone(self.session.get(ChallengeKnowledgeTag, (self.challenge.id, tag_id)))
        self.assertEqual(self.session.get(KnowledgeRelation, self.relation.id).status, "approved")
        self.assertEqual(self.session.scalars(select(KnowledgeTagDeletionArchive)).all(), [])
        self.assertEqual(self.session.scalars(select(KnowledgeRelationReview)).all(), [])
        self.assertEqual(self.session.scalars(select(KnowledgeProjectionOutbox)).all(), [])

    def test_search_private_source_paging_literal_wildcards_and_ids(self):
        self.session.add_all([Challenge(name="100%_ literal", description="x", category="web", difficulty="beginner"),
            Challenge(name="100AA literal", description="x", category="web", difficulty="beginner")])
        self.session.commit()
        response = self.run_async(knowledge_contents.search_knowledge_contents("challenge", "100%_", 0, 20, self.db, self.actor, "admin"))
        self.assertEqual(response.total, 1)
        self.assertEqual(response.items[0].name, "100%_ literal")
        self.assertFalse(response.items[0].is_active)
        self.assertIsNone(response.items[0].href)
        page = self.run_async(knowledge_contents.search_knowledge_contents("challenge", "", 1, 1, self.db, self.actor, "admin"))
        self.assertEqual(page.total, 3)
        self.assertEqual(len(page.items), 1)
        by_id = self.run_async(knowledge_contents.search_knowledge_contents("course", str(self.course.id), 0, 20, self.db, self.actor, "admin"))
        self.assertEqual([item.id for item in by_id.items], [str(self.course.id)])
        empty = self.run_async(knowledge_contents.search_knowledge_contents("course", "없음", 0, 20, self.db, self.actor, "admin"))
        self.assertEqual(empty.total, 0)
        self.assertEqual(empty.items, [])
        huge = self.run_async(knowledge_contents.search_knowledge_contents("challenge", "9" * 200, 0, 20, self.db, self.actor, "admin"))
        self.assertEqual(huge.total, 0)

    def test_hint_search_uses_label_not_secret_body(self):
        self.session.add(Hint(challenge_id=self.challenge.id, content="secret-only", order_index=0, cost=0))
        self.session.commit()
        found = self.run_async(knowledge_contents.search_knowledge_contents("hint", "비공개 문제", 0, 20, self.db, self.actor, "admin"))
        self.assertEqual(found.total, 1)
        self.assertEqual(found.items[0].name, "비공개 문제 · 힌트 1")
        secret = self.run_async(knowledge_contents.search_knowledge_contents("hint", "secret-only", 0, 20, self.db, self.actor, "admin"))
        self.assertEqual(secret.total, 0)

    def test_query_is_bounded_and_parameterized_for_all_types(self):
        for kind in ("challenge", "course", "lesson", "knowledge_tag", "hint", "event", "assignment"):
            statement = knowledge_contents.content_search_query(kind, "' OR 1=1 --").offset(20).limit(10)
            compiled = statement.compile(dialect=dialect())
            sql = str(compiled)
            self.assertIn("LIMIT", sql)
            self.assertIn("OFFSET", sql)
            self.assertNotIn("' OR 1=1 --", sql)

    def test_api_roles_validation_and_preview_readonly(self):
        app = FastAPI()
        app.include_router(knowledge_tags.router)
        app.include_router(knowledge_contents.router)
        app.dependency_overrides[get_db] = lambda: self.db
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                self.assertEqual((await client.get(f"/knowledge-tags/{self.tag.id}/deletion-preview")).status_code, 401)
                self.assertEqual((await client.get("/admin/knowledge-contents?type=course")).status_code, 401)
                self.assertEqual((await client.delete(f"/knowledge-tags/{self.tag.id}")).status_code, 401)
                with patch.object(deps, "_decode_bearer_token", return_value={"sub": str(self.actor)}):
                    for role in ("user", "student", "professor"):
                        with patch.object(deps, "_require_current_authorization", new=AsyncMock(return_value=role)):
                            self.assertEqual((await client.get(f"/knowledge-tags/{self.tag.id}/deletion-preview")).status_code, 403)
                            self.assertEqual((await client.get("/admin/knowledge-contents?type=course")).status_code, 403)
                            self.assertEqual((await client.delete(f"/knowledge-tags/{self.tag.id}")).status_code, 403)
                    for role in ("admin", "moderator"):
                        with patch.object(deps, "_require_current_authorization", new=AsyncMock(return_value=role)):
                            response = await client.get(f"/knowledge-tags/{self.tag.id}/deletion-preview")
                            self.assertEqual(response.status_code, 200)
                            self.assertEqual(len(response.json()["confirmation"]), 64)
                            self.assertEqual((await client.get("/admin/knowledge-contents?type=course")).status_code, 200)
                            for params in ("type=invalid", "type=course&offset=-1", "type=course&limit=0", "type=course&limit=101", "type=course&q=%00"):
                                self.assertEqual((await client.get("/admin/knowledge-contents?" + params)).status_code, 422)
                            self.assertEqual((await client.delete(f"/knowledge-tags/{self.tag.id}?confirmation=bad")).status_code, 422)
        self.run_async(run())
        self.assertEqual(self.db.commits, 0)
        self.assertEqual(self.session.scalars(select(KnowledgeTagDeletionArchive)).all(), [])

    def test_impact_locks_are_nowait_and_deleted_audit_has_no_foreign_key(self):
        statements = []
        original = self.db.scalars
        async def observe(query):
            statements.append(query)
            return await original(query)
        with patch.object(self.db, "scalars", side_effect=observe):
            self.run_async(load_tag_deletion_impact(self.db, self.tag.id, lock=True))
        for statement in statements:
            self.assertIn("FOR UPDATE NOWAIT", str(statement.compile(dialect=dialect())))
        self.assertEqual(list(KnowledgeTagMappingAuditEvent.__table__.c.tag_id.foreign_keys), [])

    def test_migration_preserves_ledger_values_and_blocks_destructive_downgrade(self):
        path = SERVICE / "migrations/versions/20261020_032_knowledge_tag_deletion_archive.py"
        spec = importlib.util.spec_from_file_location("tag_delete_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        for foreign_key_name in ("fk_knowledge_tag_mapping_audit_tag", "knowledge_tag_mapping_audit_events_tag_id_fkey"):
            with patch.object(migration, "op") as operations, patch.object(migration.sa, "inspect") as inspector:
                inspector.return_value.get_foreign_keys.return_value = [{"name": foreign_key_name,
                    "constrained_columns": ["tag_id"], "referred_table": "knowledge_tags", "referred_columns": ["id"]}]
                migration.upgrade()
                operations.drop_constraint.assert_any_call(foreign_key_name, "knowledge_tag_mapping_audit_events", type_="foreignkey")
                operations.execute.assert_not_called()
                operations.drop_table.assert_not_called()
        with patch.object(migration, "op") as operations, patch.object(migration.sa, "inspect") as inspector:
            inspector.return_value.get_foreign_keys.return_value = []
            with self.assertRaisesRegex(RuntimeError, "유일하게"):
                migration.upgrade()
            operations.drop_constraint.assert_not_called()
        with self.assertRaises(RuntimeError):
            migration.downgrade()


if __name__ == "__main__":
    unittest.main()
