"""SQLite transaction behavior; PostgreSQL row-lock acceptance is separate."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.api.lessons import update_lesson
from app.api.challenges import update_challenge
from app.api.courses import update_course_metadata
from app.models.challenge import Challenge, Hint
from app.models.challenge_attachment import ChallengeAttachment, ChallengeAttachmentDraft
from app.models.course import Course, CourseAuditEvent, Lesson
from app.models.knowledge import KnowledgeTag, CourseKnowledgeTag, LessonKnowledgeTag, ChallengeKnowledgeTag, KnowledgeTagMappingAuditEvent
from app.schemas.challenge import ChallengeUpdate
from app.schemas.course import CourseMetadataUpdate, LessonUpdate
from app.schemas.knowledge import ContentTagMappingItem, ContentTagMappingUpdate
from app.utils.content_tag_mappings import get_content_tag_mapping
from app.utils.course_metadata import course_metadata_revision


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(_type, _compiler, **_kwargs):
    return "JSON"


class _AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session
        self.commits = 0

    async def execute(self, statement):
        return self.session.execute(statement)

    async def scalars(self, statement):
        return self.session.scalars(statement)

    def add(self, item):
        self.session.add(item)

    async def delete(self, item):
        self.session.delete(item)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.commits += 1
        self.session.commit()

    async def refresh(self, item):
        self.session.refresh(item)


class ContentEditorMappingTransactionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        for model in (Course, CourseAuditEvent, Lesson, KnowledgeTag, CourseKnowledgeTag, LessonKnowledgeTag, KnowledgeTagMappingAuditEvent, Challenge, Hint, ChallengeKnowledgeTag, ChallengeAttachmentDraft, ChallengeAttachment):
            model.__table__.create(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)
        self.db = _AsyncSessionAdapter(self.session)
        self.actor_id = uuid4()
        self.course = Course(id=uuid4(), title="Course", category="web", difficulty="basic", author_id=self.actor_id)
        self.lesson = Lesson(id=uuid4(), course_id=self.course.id, title="Before", content="<p>Before</p>", attachments=[])
        self.session.add_all([self.course, self.lesson, KnowledgeTag(id=1, name="Manual", slug="manual")])
        self.session.flush()
        self.mapping = LessonKnowledgeTag(lesson_id=self.lesson.id, tag_id=1, direction="content_to_knowledge_tag",
            strength=0.4, confidence=0.8, evidence_summary="Retained", origin="model_candidate",
            created_by=self.actor_id, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), approved_at=None)
        self.session.add(self.mapping)
        self.session.commit()
        self.projection = patch("app.utils.content_tag_mappings.project_rule_tag_mappings", new=AsyncMock())
        self.projection.start()
        self.route_projection = patch("app.api.lessons.project_rule_tag_mappings", new=AsyncMock())
        self.route_projection.start()
        self.challenge_projection = patch("app.api.challenges.project_rule_tag_mappings", new=AsyncMock())
        self.challenge_projection.start()

    def tearDown(self):
        self.projection.stop()
        self.route_projection.stop()
        self.challenge_projection.stop()
        self.session.close()
        self.engine.dispose()

    def run_update(self, body):
        return asyncio.run(update_lesson(course_id=self.course.id, lesson_id=self.lesson.id,
            body=body, db=self.db, _user_id=self.actor_id, _role="admin"))

    def structured_input(self, **changes):
        current = asyncio.run(get_content_tag_mapping(self.db, entity_type="lesson", entity_id=self.lesson.id))
        return ContentTagMappingUpdate(expected_revision=current.mapping_revision,
            mappings=[ContentTagMappingItem(tag_id=1, strength=0.6, confidence=0.9, evidence_summary="New evidence")], **changes)

    def test_body_and_quality_commit_together_once_without_approving_candidate(self):
        self.run_update(LessonUpdate(title="After", knowledge_tag_mapping=self.structured_input()))
        self.assertEqual(self.db.commits, 1)
        self.session.expire_all()
        self.assertEqual(self.session.get(Lesson, self.lesson.id).title, "After")
        row = self.session.get(LessonKnowledgeTag, (self.lesson.id, 1))
        self.assertEqual(float(row.strength), 0.6)
        self.assertEqual(row.evidence_summary, "New evidence")
        self.assertEqual(row.origin, "model_candidate")
        self.assertIsNone(row.approved_at)

    def test_revision_conflict_does_not_commit_body_or_quality(self):
        mapping_input = self.structured_input()
        mapping_input.expected_revision = "0" * 64
        with self.assertRaises(HTTPException) as caught:
            self.run_update(LessonUpdate(title="Lost update", knowledge_tag_mapping=mapping_input))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.db.commits, 0)
        self.session.rollback()
        self.assertEqual(self.session.get(Lesson, self.lesson.id).title, "Before")
        self.assertEqual(float(self.session.get(LessonKnowledgeTag, (self.lesson.id, 1)).strength), 0.4)

    def test_legacy_ids_preserve_quality_and_pending_approval(self):
        self.run_update(LessonUpdate(title="After", knowledge_tag_ids=[1]))
        row = self.session.get(LessonKnowledgeTag, (self.lesson.id, 1))
        self.assertEqual(float(row.strength), 0.4)
        self.assertEqual(row.evidence_summary, "Retained")
        self.assertIsNone(row.approved_at)

    def test_omitted_mapping_input_preserves_mapping_without_audit(self):
        self.run_update(LessonUpdate(title="After"))
        row = self.session.get(LessonKnowledgeTag, (self.lesson.id, 1))
        self.assertEqual(float(row.strength), 0.4)
        self.assertIsNone(row.approved_at)
        self.assertEqual(self.session.scalars(select(KnowledgeTagMappingAuditEvent)).all(), [])

    def test_lesson_and_challenge_reject_ambiguous_dual_mapping_input(self):
        for schema in (LessonUpdate, ChallengeUpdate):
            with self.assertRaises(ValidationError):
                schema(knowledge_tag_ids=[], knowledge_tag_mapping=ContentTagMappingUpdate(mappings=[]))

    def test_legacy_invalid_ids_are_request_validation_errors(self):
        for schema in (LessonUpdate, ChallengeUpdate):
            for ids in ([-1], [0], [1, 1], list(range(1, 52))):
                with self.assertRaises(ValidationError):
                    schema(knowledge_tag_ids=ids)

    def create_challenge(self):
        challenge = Challenge(name="Before", category="web", difficulty="beginner", description="<p>Before</p>", env_type="none")
        self.session.add(challenge)
        self.session.flush()
        self.session.add(ChallengeKnowledgeTag(challenge_id=challenge.id, tag_id=1, origin="import",
            strength=0.4, confidence=0.8, evidence_summary="Retained", created_by=self.actor_id,
            approved_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        self.session.commit()
        return challenge

    def run_challenge_update(self, challenge, body):
        return asyncio.run(update_challenge(challenge_id=challenge.id, body=body, background_tasks=BackgroundTasks(),
            db=self.db, actor_id=self.actor_id, _role="admin"))

    def test_challenge_body_and_quality_commit_together_once(self):
        challenge = self.create_challenge()
        current = asyncio.run(get_content_tag_mapping(self.db, entity_type="challenge", entity_id=challenge.id))
        self.run_challenge_update(challenge, ChallengeUpdate(name="After", knowledge_tag_mapping=ContentTagMappingUpdate(
            expected_revision=current.mapping_revision,
            mappings=[ContentTagMappingItem(tag_id=1, strength=0.7, confidence=0.9, evidence_summary="Updated")],
        )))
        self.assertEqual(self.db.commits, 1)
        self.session.expire_all()
        self.assertEqual(self.session.get(Challenge, challenge.id).name, "After")
        self.assertEqual(float(self.session.get(ChallengeKnowledgeTag, (challenge.id, 1)).strength), 0.7)
        self.assertEqual(self.session.get(Challenge, challenge.id).attachment_revision, 0)
        self.assertEqual(self.session.scalars(select(ChallengeAttachment)).all(), [])
        self.assertEqual(self.session.scalars(select(ChallengeAttachmentDraft)).all(), [])

    def test_challenge_conflict_keeps_body_and_mapping_unchanged(self):
        challenge = self.create_challenge()
        with self.assertRaises(HTTPException) as caught:
            self.run_challenge_update(challenge, ChallengeUpdate(name="Lost update", knowledge_tag_mapping=ContentTagMappingUpdate(
                expected_revision="0" * 64, mappings=[])))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.db.commits, 0)
        self.session.rollback()
        self.assertEqual(self.session.get(Challenge, challenge.id).name, "Before")
        self.assertEqual(float(self.session.get(ChallengeKnowledgeTag, (challenge.id, 1)).strength), 0.4)

    def test_challenge_ordinary_patch_keeps_mapping_quality(self):
        challenge = self.create_challenge()
        self.run_challenge_update(challenge, ChallengeUpdate(name="After"))
        row = self.session.get(ChallengeKnowledgeTag, (challenge.id, 1))
        self.assertEqual(float(row.strength), 0.4)
        self.assertEqual(row.origin, "import")
        self.assertEqual(challenge.attachment_revision, 0)
        self.assertEqual(challenge.files, [])
        self.assertEqual(self.session.scalars(select(ChallengeAttachment)).all(), [])

    def create_course_mapping(self):
        self.session.add(CourseKnowledgeTag(course_id=self.course.id, tag_id=1, origin="manual",
            strength=0.4, confidence=0.8, evidence_summary="Retained", created_by=self.actor_id,
            approved_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        self.session.commit()

    def run_course_update(self, **fields):
        return asyncio.run(update_course_metadata(course_id=self.course.id, body=CourseMetadataUpdate(
            expected_metadata_revision=course_metadata_revision(self.course.title, self.course.description), **fields),
            db=self.db, actor_id=self.actor_id, _role="admin"))

    def test_course_body_and_quality_commit_together_once(self):
        self.create_course_mapping()
        current = asyncio.run(get_content_tag_mapping(self.db, entity_type="course", entity_id=self.course.id))
        self.run_course_update(title="After", knowledge_tag_mapping=ContentTagMappingUpdate(
            expected_revision=current.mapping_revision,
            mappings=[ContentTagMappingItem(tag_id=1, strength=0.7, confidence=0.9, evidence_summary="Updated")]))
        self.assertEqual(self.db.commits, 1)
        self.session.expire_all()
        self.assertEqual(self.session.get(Course, self.course.id).title, "After")
        self.assertEqual(float(self.session.get(CourseKnowledgeTag, (self.course.id, 1)).strength), 0.7)

    def test_course_mapping_conflict_does_not_commit_body_or_audit(self):
        self.create_course_mapping()
        with self.assertRaises(HTTPException) as caught:
            self.run_course_update(title="Lost update", knowledge_tag_mapping=ContentTagMappingUpdate(
                expected_revision="0" * 64, mappings=[]))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.db.commits, 0)
        self.session.rollback()
        self.assertEqual(self.session.get(Course, self.course.id).title, "Course")
        self.assertEqual(self.session.scalars(select(CourseAuditEvent)).all(), [])

    def test_course_ordinary_patch_preserves_mapping_without_mapping_audit(self):
        self.create_course_mapping()
        self.run_course_update(title="After")
        row = self.session.get(CourseKnowledgeTag, (self.course.id, 1))
        self.assertEqual(float(row.strength), 0.4)
        self.assertEqual(self.session.scalars(select(KnowledgeTagMappingAuditEvent)).all(), [])


if __name__ == "__main__":
    unittest.main()
