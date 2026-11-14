"""작업 전용 PostgreSQL URL과 UUID schema를 사용하는 지식 통합 인수."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.course import Base, Course, CourseAuditEvent, CoursePrerequisite, Lesson, UserProgress
import app.models.education
import app.models.event
import app.models.challenge_submission
import app.models.lesson_image
import app.models.challenge_image_cleanup
from app.models.challenge import Challenge, ChallengePrerequisite
from app.models.knowledge import (ChallengeKnowledgeTag, KnowledgeContentNode, KnowledgeGraphRevision,
    KnowledgeRelation, KnowledgeRelationProposal, KnowledgeTag, KnowledgeTagMappingAuditEvent,
    CourseKnowledgeTag, LessonKnowledgeTag)
from app.api.knowledge_graph import get_knowledge_graph, get_knowledge_map_v2_canvas, get_knowledge_map_v2_focus, _v2_graph_revision
from app.api.knowledge_relations import (bulk_review_knowledge_proposals, create_knowledge_proposal,
    create_knowledge_relation, review_knowledge_proposal, review_knowledge_relation, update_knowledge_relation)
from app.api.recommend import get_personalized_recommendations, update_recommendation_consent
from app.api.lessons import get_lesson, update_lesson
from app.api.courses import _validate_course_prerequisite_replace, replace_course_prerequisites
from app.schemas.knowledge import (ContentTagMappingUpdate, KnowledgeProposalBulkReviewRequest,
    KnowledgeProposalReviewRequest, KnowledgeRelationCreate, KnowledgeRelationProposalCreate,
    KnowledgeRelationReviewRequest, KnowledgeRelationUpdate, RecommendationConsentUpdate)
from app.schemas.course import CoursePrerequisiteReplace, LessonUpdate
from app.services.knowledge_candidate_generation import (ScheduledCandidateRunnerSettings,
    generate_rule_based_candidate_run, release_scheduled_candidate_runner_lock,
    try_acquire_scheduled_candidate_runner_lock)
from app.utils.content_tag_mappings import get_content_tag_mapping, replace_content_tag_mappings
from app.utils.knowledge_tag_projection import project_rule_tag_mappings
from app.utils.course_prerequisite_rules import would_create_course_prerequisite_cycle
from app.workers.knowledge_candidate_runner import KnowledgeCandidateRunner


DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")


@unittest.skipUnless(DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL 작업 DB가 필요합니다.")
class KnowledgePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "knowledge_test_" + uuid4().hex
        self.admin_engine = create_async_engine(DATABASE_URL)
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_async_engine(DATABASE_URL, connect_args={"server_settings": {"search_path": self.schema}}, pool_size=5)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            def install(sync_connection):
                path = SERVICE_ROOT / "migrations" / "versions" / "20261013_028_knowledge_mapping_ownership_revision.py"
                spec = importlib.util.spec_from_file_location("knowledge_test_migration", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                with Operations.context(MigrationContext.configure(sync_connection)):
                    module.upgrade()
            await connection.run_sync(install)
        self.author, self.reviewer, self.learner = uuid4(), uuid4(), uuid4()

    async def asyncTearDown(self):
        await self.engine.dispose()
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        await self.admin_engine.dispose()

    def session(self):
        return AsyncSession(self.engine, expire_on_commit=False)

    async def challenges(self, count=2):
        async with self.session() as db:
            items = [Challenge(name=f"Task {identifier}", category="web", challenge_type="wargame", is_published=True)
                     for identifier in range(1, count + 1)]
            db.add_all(items)
            await db.commit()
            return [item.id for item in items]

    async def new_relation(self, db, source, target, **values):
        return await create_knowledge_relation(KnowledgeRelationCreate(source={"type": "challenge", "id": str(source)},
            target={"type": "challenge", "id": str(target)}, relation_type="helps", evidence_summary="공개 도움 관계", **values),
            db=db, actor_id=self.author, _role="admin")

    async def test_legacy_independent_approval_and_revision_keeps_public_until_review(self):
        source, target = await self.challenges()
        async with self.session() as db:
            relation = await self.new_relation(db, source, target)
            with self.assertRaises(HTTPException) as caught:
                await review_knowledge_relation(relation.id, KnowledgeRelationReviewRequest(action="approve", reason="직접 검토"), db=db, actor_id=self.author, _role="admin")
            self.assertEqual(caught.exception.status_code, 403)
            await db.rollback()
            relation = await review_knowledge_relation(relation.id, KnowledgeRelationReviewRequest(action="approve", reason="독립 검토"), db=db, actor_id=self.reviewer, _role="admin")
            edited = await update_knowledge_relation(relation.id, KnowledgeRelationUpdate(strength=0.4, reason="품질 개정 요청"), db=db, actor_id=self.author, _role="admin")
            self.assertEqual(edited.status, "approved")
            self.assertEqual(edited.strength, 1)
            self.assertIsNotNone(edited.pending_proposal_id)
            proposal = await review_knowledge_proposal(edited.pending_proposal_id, KnowledgeProposalReviewRequest(action="approve", reason="개정 독립 검토"), db=db, actor_id=self.reviewer, _role="admin")
            old = await db.get(KnowledgeRelation, relation.id, populate_existing=True)
            new = await db.get(KnowledgeRelation, proposal.published_relation_id)
            self.assertEqual(old.status, "retired")
            self.assertEqual(new.status, "approved")
            self.assertEqual(float(new.strength), 0.4)

    async def test_owned_tag_mapping_first_approval_retire_and_manual_preservation(self):
        challenge_id = (await self.challenges(1))[0]
        async with self.session() as db:
            tag = KnowledgeTag(name="New taxonomy", slug="new-taxonomy")
            db.add(tag)
            await db.commit()
            body = KnowledgeRelationProposalCreate(source={"type": "challenge", "id": str(challenge_id)},
                target={"type": "knowledge_tag", "id": str(tag.id)}, relation_type="tag_affinity", origin="rule",
                strength=0.8, confidence=0.9, evidence_summary="명시 메타데이터 후보",
                evidence=[{"evidence_kind": "rule", "summary": "태그 후보 독립 검토"}])
            proposal = await create_knowledge_proposal(body, db=db, actor_id=self.author, _role="admin")
            with self.assertRaises(HTTPException) as caught:
                await review_knowledge_proposal(proposal.id, KnowledgeProposalReviewRequest(action="approve", reason="자기 검토 거부"), db=db, actor_id=self.author, _role="admin")
            self.assertEqual(caught.exception.status_code, 403)
            await db.rollback()
            approved = await review_knowledge_proposal(proposal.id, KnowledgeProposalReviewRequest(action="approve", reason="독립 승인 완료"), db=db, actor_id=self.reviewer, _role="admin")
            mapping = await db.get(ChallengeKnowledgeTag, (challenge_id, tag.id))
            self.assertEqual(mapping.source_relation_id, approved.published_relation_id)
            revision = await get_content_tag_mapping(db, entity_type="challenge", entity_id=challenge_id)
            response = await replace_content_tag_mappings(db, entity_type="challenge", entity=await db.get(Challenge, challenge_id),
                body=ContentTagMappingUpdate(mappings=[], expected_revision=revision.mapping_revision), actor_id=self.author)
            self.assertEqual(response.tag_ids, [tag.id])
            await db.commit()
            await review_knowledge_proposal(proposal.id, KnowledgeProposalReviewRequest(action="retire", reason="자동 관계 은퇴"), db=db, actor_id=self.reviewer, _role="admin")
            self.assertIsNone(await db.get(ChallengeKnowledgeTag, (challenge_id, tag.id), populate_existing=True))
            self.assertGreaterEqual(await db.scalar(select(func.count()).select_from(KnowledgeTagMappingAuditEvent)), 2)

    async def test_mapping_stale_revision_is_atomic_and_id_only_preserves_quality(self):
        challenge_id = (await self.challenges(1))[0]
        async with self.session() as db:
            tag = KnowledgeTag(name="Manual taxonomy", slug="manual-taxonomy")
            db.add(tag)
            await db.flush()
            mapping = ChallengeKnowledgeTag(challenge_id=challenge_id, tag_id=tag.id, strength=0.2, confidence=0.3,
                evidence_summary="수동 근거 보존", origin="import", created_by=self.author)
            db.add(mapping)
            await db.commit()
            original = await get_content_tag_mapping(db, entity_type="challenge", entity_id=challenge_id)
            tag_id = tag.id
            body = ContentTagMappingUpdate(mappings=[{"tag_id": tag.id, "strength": 0.7, "confidence": 0.8, "evidence_summary": "변경 품질"}], expected_revision=original.mapping_revision)
            await replace_content_tag_mappings(db, entity_type="challenge", entity=await db.get(Challenge, challenge_id), body=body, actor_id=self.author)
            await db.commit()
            with self.assertRaises(HTTPException) as caught:
                await replace_content_tag_mappings(db, entity_type="challenge", entity=await db.get(Challenge, challenge_id), body=ContentTagMappingUpdate(mappings=[], expected_revision=original.mapping_revision), actor_id=self.reviewer)
            self.assertEqual(caught.exception.status_code, 409)
            await db.rollback()
            response = await replace_content_tag_mappings(db, entity_type="challenge", entity=await db.get(Challenge, challenge_id), body=ContentTagMappingUpdate(tag_ids=[tag_id]), actor_id=self.author)
            self.assertEqual(response.mappings[0].strength, 0.7)
            self.assertEqual(response.mappings[0].origin, "import")

    async def test_mvcc_revision_records_out_of_order_commits_without_global_lock(self):
        async with self.session() as first, self.session() as second:
            first.add(Challenge(name="First", category="web", is_published=True))
            await first.flush()
            second.add(Challenge(name="Second", category="web", is_published=True))
            await asyncio.wait_for(second.flush(), timeout=3)
            await second.commit()
            async with self.session() as reader:
                before = await _v2_graph_revision(reader)
            await first.commit()
            async with self.session() as reader:
                after = await _v2_graph_revision(reader)
            self.assertNotEqual(before, after)

    async def test_keyset_pages_and_stale_cursor_with_actual_postgresql_sql(self):
        ids = await self.challenges(130)
        async with self.session() as db:
            tag = KnowledgeTag(name="Web", slug="web")
            db.add(tag)
            await db.flush()
            db.add_all([ChallengeKnowledgeTag(challenge_id=identifier, tag_id=tag.id, origin="manual", strength=0.8, confidence=0.9) for identifier in ids])
            await db.commit()
            first = await get_knowledge_map_v2_canvas(types=None, relation_types=None, cursor=None, db=db, viewer_id=None)
            second = await get_knowledge_map_v2_canvas(types=None, relation_types=None, cursor=first.next_cursor, db=db, viewer_id=None)
            self.assertEqual(len({edge.id for edge in first.edges + second.edges}), 130)
            self.assertIsNone(second.next_cursor)
            for page in (first, second):
                keys = {node.id for node in page.nodes}
                self.assertLessEqual(len(keys), 120)
                self.assertTrue(all(edge.source in keys and edge.target in keys for edge in page.edges))
            focus = await get_knowledge_map_v2_focus(focus_type="challenge", focus_id=str(ids[0]), depth=2,
                types=None, relation_types=None, continuation=None, cursor=None, db=db, viewer_id=None)
            self.assertEqual(focus.focus.id, f"challenge:{ids[0]}")
            self.assertIsNotNone(focus.next_cursor)
            node = await db.get(Challenge, ids[0])
            node.name = "Updated title"
            await db.commit()
            with self.assertRaises(HTTPException) as caught:
                await get_knowledge_map_v2_canvas(types=None, relation_types=None, cursor=first.next_cursor, db=db, viewer_id=None)
            self.assertEqual(caught.exception.status_code, 409)

    async def test_runner_reuses_physical_connection_and_releases_on_success_failure_cancel(self):
        settings = ScheduledCandidateRunnerSettings(enabled=True, interval_seconds=60, max_candidates=10, execution_principal="test-runner")
        pids = []
        async def generate(db, *, invocation):
            pids.append(await db.scalar(select(func.pg_backend_pid())))
            async with self.engine.connect() as other:
                async with AsyncSession(bind=other) as competitor:
                    self.assertFalse(await try_acquire_scheduled_candidate_runner_lock(competitor))
            result = await generate_rule_based_candidate_run(db, invocation=invocation)
            pids.append(await db.scalar(select(func.pg_backend_pid())))
            return result
        with patch("app.workers.knowledge_candidate_runner.engine", self.engine), patch("app.workers.knowledge_candidate_runner.generate_rule_based_candidate_run", generate):
            self.assertEqual(await KnowledgeCandidateRunner(settings).run_once(), "succeeded")
        self.assertEqual(pids[0], pids[1])
        async def check_free():
            async with self.engine.connect() as connection:
                async with AsyncSession(bind=connection) as db:
                    self.assertTrue(await try_acquire_scheduled_candidate_runner_lock(db))
                    await release_scheduled_candidate_runner_lock(db)
        await check_free()
        async def failing(*args, **kwargs):
            raise RuntimeError("task-owned injected failure")
        with patch("app.workers.knowledge_candidate_runner.engine", self.engine), patch("app.workers.knowledge_candidate_runner.generate_rule_based_candidate_run", failing):
            self.assertEqual(await KnowledgeCandidateRunner(settings).run_once(), "retry")
        await check_free()
        entered = asyncio.Event()
        async def waiting(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch("app.workers.knowledge_candidate_runner.engine", self.engine), patch("app.workers.knowledge_candidate_runner.generate_rule_based_candidate_run", waiting):
            task = asyncio.create_task(KnowledgeCandidateRunner(settings).run_once())
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await check_free()

    async def test_recommendation_consent_valid_target_score_and_public_next_action(self):
        ids = await self.challenges(4)
        async with self.session() as db:
            response = await get_personalized_recommendations(limit=1, db=db, user_id=self.learner)
            self.assertTrue(response.consent_required)
            await update_recommendation_consent(RecommendationConsentUpdate(enabled=True), db=db, user_id=self.learner)
            response = await get_personalized_recommendations(limit=1, db=db, user_id=self.learner)
            self.assertEqual(response.empty_reason, "no_history")
            course = Course(title="Source course", category="web", difficulty="basic", author_id=self.author,
                            status="approved", is_published=True)
            db.add(course)
            await db.flush()
            lesson = Lesson(course_id=course.id, title="Completed source")
            db.add(lesson)
            await db.flush()
            db.add(UserProgress(user_id=self.learner, lesson_id=lesson.id, completed=True))
            source = KnowledgeContentNode(entity_type="lesson", entity_id=str(lesson.id), display_name=lesson.title, is_active=True)
            db.add(source)
            await db.flush()
            for identifier, strength, confidence in ((ids[0], 1, 1), (ids[1], 0.6, 1), (ids[2], 1, 0.2)):
                target = KnowledgeContentNode(entity_type="challenge", entity_id=str(identifier), display_name=f"Target {identifier}", is_active=True)
                db.add(target)
                await db.flush()
                db.add(KnowledgeRelation(source_node_id=source.id, target_node_id=target.id, relation_type="helps",
                    is_directed=True, strength=strength, confidence=confidence, origin="manual", status="approved",
                    created_by=self.author, evidence_summary="공개 추천 근거"))
            hidden = await db.get(Challenge, ids[0])
            hidden.is_published = False
            db.add(ChallengePrerequisite(challenge_id=ids[1], prerequisite_id=ids[3]))
            await db.commit()
            response = await get_personalized_recommendations(limit=1, db=db, user_id=self.learner)
            self.assertEqual([item.id for item in response.items], [str(ids[1])])
            self.assertEqual(response.items[0].score, 0.6)
            self.assertTrue(response.items[0].is_locked)
            self.assertEqual(response.items[0].next_action.id, str(ids[3]))
            await update_recommendation_consent(RecommendationConsentUpdate(enabled=False), db=db, user_id=self.learner)
            withdrawn = await get_personalized_recommendations(limit=1, db=db, user_id=self.learner)
            self.assertTrue(withdrawn.consent_required)
            self.assertFalse(withdrawn.items)

    async def test_legacy_public_evidence_redaction(self):
        source, target = await self.challenges()
        async with self.session() as db:
            relation = await create_knowledge_relation(KnowledgeRelationCreate(source={"type": "challenge", "id": str(source)},
                target={"type": "challenge", "id": str(target)}, relation_type="helps", evidence_summary="CTF{fixture-secret}"),
                db=db, actor_id=self.author, _role="admin")
            await review_knowledge_relation(relation.id, KnowledgeRelationReviewRequest(action="approve", reason="독립 검토 승인"), db=db, actor_id=self.reviewer, _role="admin")
            graph = await get_knowledge_graph(db=db, user_id=None)
            self.assertTrue(any(edge.evidence_summary == "관리자가 승인한 관계 근거" for edge in graph.edges))
            self.assertNotIn("fixture-secret", graph.model_dump_json())
            from app.utils.knowledge_relations import get_approved_helpful_content
            helpful = await get_approved_helpful_content(target, db)
            self.assertEqual(helpful[0]["evidence_summary"], "관리자가 승인한 관계 근거")

    async def test_course_candidate_inheritance_retire_keeps_manual_lesson_mapping(self):
        async with self.session() as db:
            course = Course(title="Inheritance", category="web", difficulty="basic", author_id=self.author,
                            status="approved", is_published=True)
            db.add(course)
            tag = KnowledgeTag(name="Candidate taxonomy", slug="candidate-taxonomy")
            manual_tag = KnowledgeTag(name="Manual lesson taxonomy", slug="manual-lesson")
            db.add_all([tag, manual_tag])
            await db.flush()
            lesson = Lesson(course_id=course.id, title="Lesson inheritance")
            db.add(lesson)
            await db.flush()
            course_id, lesson_id, tag_id, manual_tag_id = course.id, lesson.id, tag.id, manual_tag.id
            db.add(LessonKnowledgeTag(lesson_id=lesson_id, tag_id=manual_tag_id, origin="manual", created_by=self.author))
            await db.commit()
            proposal = await create_knowledge_proposal(KnowledgeRelationProposalCreate(
                source={"type": "course", "id": str(course_id)}, target={"type": "knowledge_tag", "id": str(tag_id)},
                relation_type="tag_affinity", origin="rule", evidence_summary="공개 강의 태그 후보",
                evidence=[{"evidence_kind": "rule", "summary": "강의 태그 후보 근거"}]),
                db=db, actor_id=self.author, _role="admin")
            approved = await review_knowledge_proposal(proposal.id, KnowledgeProposalReviewRequest(action="approve", reason="강의 후보 독립 승인"),
                                                      db=db, actor_id=self.reviewer, _role="admin")
            inherited = await db.get(LessonKnowledgeTag, (lesson_id, tag_id))
            self.assertEqual(inherited.origin, "rule")
            self.assertIsNone(inherited.source_relation_id)
            owner = await db.get(CourseKnowledgeTag, (course_id, tag_id))
            self.assertEqual(owner.source_relation_id, approved.published_relation_id)
            await review_knowledge_proposal(proposal.id, KnowledgeProposalReviewRequest(action="retire", reason="강의 후보 관계 은퇴"),
                                           db=db, actor_id=self.reviewer, _role="admin")
            self.assertIsNone(await db.get(CourseKnowledgeTag, (course_id, tag_id), populate_existing=True))
            self.assertIsNone(await db.get(LessonKnowledgeTag, (lesson_id, tag_id), populate_existing=True))
            self.assertIsNotNone(await db.get(LessonKnowledgeTag, (lesson_id, manual_tag_id), populate_existing=True))

    async def test_bulk_review_commits_success_and_reports_missing_item(self):
        ids = await self.challenges(2)
        async with self.session() as db:
            proposal = await create_knowledge_proposal(KnowledgeRelationProposalCreate(
                source={"type": "challenge", "id": str(ids[0])}, target={"type": "challenge", "id": str(ids[1])},
                relation_type="helps", evidence_summary="도움 관계 후보",
                evidence=[{"evidence_kind": "manual", "summary": "도움 관계 검토 근거"}]),
                db=db, actor_id=self.author, _role="admin")
            missing_id = uuid4()
            response = await bulk_review_knowledge_proposals(KnowledgeProposalBulkReviewRequest(
                proposal_ids=[proposal.id, missing_id], action="approve", reason="항목별 독립 검토"),
                db=db, actor_id=self.reviewer, _role="admin")
            self.assertEqual([entry["status"] for entry in response["items"]], ["succeeded", "failed"])
            self.assertEqual(response["items"][1]["http_status"], 404)
            persisted = await db.get(KnowledgeRelationProposal, proposal.id)
            self.assertEqual(persisted.state, "approved")

    async def test_concurrent_mapping_writers_reject_stale_revision(self):
        challenge_id = (await self.challenges(1))[0]
        async with self.session() as setup:
            tags = [KnowledgeTag(name=f"Manual {index}", slug=f"manual-{index}") for index in (1, 2)]
            setup.add_all(tags)
            await setup.commit()
            tag_ids = [tag.id for tag in tags]
            revision = (await get_content_tag_mapping(setup, entity_type="challenge", entity_id=challenge_id)).mapping_revision
        async with self.session() as first, self.session() as second:
            first_entity = await first.get(Challenge, challenge_id)
            second_entity = await second.get(Challenge, challenge_id)
            await replace_content_tag_mappings(first, entity_type="challenge", entity=first_entity,
                body=ContentTagMappingUpdate(tag_ids=[tag_ids[0]], expected_revision=revision), actor_id=self.author)
            async def competing():
                try:
                    await replace_content_tag_mappings(second, entity_type="challenge", entity=second_entity,
                        body=ContentTagMappingUpdate(tag_ids=[tag_ids[1]], expected_revision=revision), actor_id=self.reviewer)
                except HTTPException as error:
                    await second.rollback()
                    return error.status_code
                return 200
            task = asyncio.create_task(competing())
            await first.commit()
            self.assertEqual(await asyncio.wait_for(task, 3), 409)
        async with self.session() as reader:
            response = await get_content_tag_mapping(reader, entity_type="challenge", entity_id=challenge_id)
            self.assertEqual(response.tag_ids, [tag_ids[0]])

    async def test_concurrent_review_creates_one_public_relation(self):
        ids = await self.challenges(2)
        async with self.session() as setup:
            proposal = await create_knowledge_proposal(KnowledgeRelationProposalCreate(
                source={"type": "challenge", "id": str(ids[0])}, target={"type": "challenge", "id": str(ids[1])},
                relation_type="prepares_for", evidence_summary="경합 검토 후보",
                evidence=[{"evidence_kind": "manual", "summary": "경합 독립 검토 근거"}]),
                db=setup, actor_id=self.author, _role="admin")
        async def approve(actor_id):
            async with self.session() as db:
                try:
                    await review_knowledge_proposal(proposal.id, KnowledgeProposalReviewRequest(action="approve", reason="경합 독립 승인"),
                                                   db=db, actor_id=actor_id, _role="admin")
                    return 200
                except HTTPException as error:
                    await db.rollback()
                    return error.status_code
        statuses = await asyncio.wait_for(asyncio.gather(approve(self.reviewer), approve(uuid4())), 5)
        self.assertEqual(sorted(statuses), [200, 409])
        async with self.session() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(KnowledgeRelation).where(KnowledgeRelation.status == "approved")), 1)

    async def course_lesson_lock_fixture(self):
        async with self.session() as db:
            course = Course(title="Lock course", category="web", difficulty="basic", author_id=self.author,
                            status="approved", is_published=True)
            tags = [KnowledgeTag(name=name, slug=slug) for name, slug in
                    (("Web lock", "web"), ("Crypto lock", "crypto"), ("Manual lock", "manual-lock"))]
            db.add_all([course, *tags])
            await db.flush()
            lesson = Lesson(title="Original lesson", course_id=course.id, content="<p>Original</p>")
            db.add(lesson)
            await db.flush()
            db.add_all([CourseKnowledgeTag(course_id=course.id, tag_id=tags[0].id, origin="rule"),
                        LessonKnowledgeTag(lesson_id=lesson.id, tag_id=tags[0].id, origin="rule"),
                        LessonKnowledgeTag(lesson_id=lesson.id, tag_id=tags[2].id, origin="manual")])
            await db.commit()
            return course.id, lesson.id, [tag.id for tag in tags]

    async def wait_until_backend_is_blocked(self, observer, waiter, blocker):
        for _ in range(100):
            blockers = await observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter})
            if blocker in blockers:
                return
            await asyncio.sleep(.02)
        self.fail("Expected PostgreSQL parent-row lock wait did not occur")

    async def test_lesson_mapping_and_projection_lock_parent_before_dirty_child(self):
        course_id, lesson_id, tags = await self.course_lesson_lock_fixture()
        for operation in ("mapping", "projection"):
            with self.subTest(operation=operation):
                async with self.session() as course_db, self.session() as lesson_db, self.session() as observer:
                    course = await course_db.scalar(select(Course).where(Course.id == course_id).with_for_update())
                    course_pid = await course_db.scalar(text("SELECT pg_backend_pid()"))
                    lesson = await lesson_db.get(Lesson, lesson_id)
                    lesson_pid = await lesson_db.scalar(text("SELECT pg_backend_pid()"))
                    lesson.title = "Dirty child after parent wait"

                    async def write_lesson():
                        if operation == "mapping":
                            await replace_content_tag_mappings(lesson_db, entity_type="lesson", entity=lesson,
                                body=ContentTagMappingUpdate(tag_ids=[tags[2]]), actor_id=self.author)
                        else:
                            await project_rule_tag_mappings(lesson_db, entity_type="lesson", entity=lesson, actor_id=self.author)
                        await lesson_db.commit()

                    task = asyncio.create_task(write_lesson())
                    try:
                        await self.wait_until_backend_is_blocked(observer, lesson_pid, course_pid)
                        # The waiter must not autoflush/lock a child before its parent.
                        await observer.execute(select(Lesson.id).where(Lesson.id == lesson_id).with_for_update(nowait=True))
                        await observer.execute(select(LessonKnowledgeTag).where(LessonKnowledgeTag.lesson_id == lesson_id).with_for_update(nowait=True))
                        await observer.rollback()
                        course.category = "crypto"
                        await replace_content_tag_mappings(course_db, entity_type="course", entity=course,
                            body=ContentTagMappingUpdate(tag_ids=[tags[1]]), actor_id=self.reviewer)
                        await course_db.commit()
                        await asyncio.wait_for(task, 5)
                    finally:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                async with self.session() as reader:
                    rows = (await get_content_tag_mapping(reader, entity_type="lesson", entity_id=lesson_id)).mappings
                    self.assertEqual({row.tag_id for row in rows}, {tags[1], tags[2]})
                    self.assertEqual({row.tag_id: row.origin for row in rows}, {tags[1]: "rule", tags[2]: "manual"})
                    self.assertEqual((await reader.get(Lesson, lesson_id)).title, "Dirty child after parent wait")

    async def test_public_lesson_get_does_not_take_an_exclusive_write_lock(self):
        course_id, lesson_id, _tags = await self.course_lesson_lock_fixture()
        async with self.session() as writer, self.session() as reader:
            lesson = await writer.scalar(select(Lesson).where(Lesson.id == lesson_id).with_for_update())
            lesson.title = "Uncommitted title"
            await writer.flush()
            response = await asyncio.wait_for(get_lesson(course_id=course_id, lesson_id=lesson_id,
                db=reader, user_id=self.learner, user_role="user"), 2)
            self.assertEqual(response.lesson.title, "Original lesson")

    async def test_lesson_update_serializes_body_and_revision_before_mutation(self):
        course_id, lesson_id, tags = await self.course_lesson_lock_fixture()
        async with self.session() as setup:
            revision = (await get_content_tag_mapping(setup, entity_type="lesson", entity_id=lesson_id)).mapping_revision
        first_replaced = asyncio.Event()
        release_first = asyncio.Event()
        original_replace = replace_content_tag_mappings
        async def paused_replace(db, **arguments):
            response = await original_replace(db, **arguments)
            if arguments["actor_id"] == self.author:
                first_replaced.set()
                await release_first.wait()
            return response
        async with self.session() as first_db, self.session() as second_db, self.session() as observer:
            first_pid = await first_db.scalar(text("SELECT pg_backend_pid()"))
            second_pid = await second_db.scalar(text("SELECT pg_backend_pid()"))
            first_input = LessonUpdate(title="Committed title", knowledge_tag_mapping=ContentTagMappingUpdate(
                mappings=[{"tag_id": tags[2], "strength": .8, "confidence": .9, "evidence_summary": "First quality"}],
                expected_revision=revision))
            second_input = LessonUpdate(estimated_minutes=99, knowledge_tag_mapping=ContentTagMappingUpdate(
                tag_ids=[tags[2]], expected_revision=revision))
            with patch("app.api.lessons.replace_content_tag_mappings", side_effect=paused_replace):
                first = asyncio.create_task(update_lesson(course_id, lesson_id, first_input,
                    db=first_db, _user_id=self.author, _role="admin"))
                await asyncio.wait_for(first_replaced.wait(), 3)
                second = asyncio.create_task(update_lesson(course_id, lesson_id, second_input,
                    db=second_db, _user_id=self.reviewer, _role="admin"))
                try:
                    await self.wait_until_backend_is_blocked(observer, second_pid, first_pid)
                    release_first.set()
                    await asyncio.wait_for(first, 5)
                    with self.assertRaises(HTTPException) as caught:
                        await asyncio.wait_for(second, 5)
                    self.assertEqual(caught.exception.status_code, 409)
                    await second_db.rollback()
                finally:
                    release_first.set()
                    for task in (first, second):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(first, second, return_exceptions=True)
        async with self.session() as reader:
            lesson = await reader.get(Lesson, lesson_id)
            self.assertEqual(lesson.title, "Committed title")
            self.assertEqual(lesson.estimated_minutes, 10)
            mapping = await reader.get(LessonKnowledgeTag, (lesson_id, tags[2]))
            self.assertEqual(float(mapping.strength), .8)

    async def prerequisite_cycle_fixture(self):
        async with self.session() as db:
            courses = [Course(title=f"Cycle {name}", category="web", difficulty="basic", author_id=self.author,
                              status="approved", is_published=True) for name in "ABCD"]
            db.add_all(courses)
            await db.flush()
            ids = [course.id for course in courses]
            a, b, c, d = ids
            db.add_all([CoursePrerequisite(course_id=b, prerequisite_id=c),
                        CoursePrerequisite(course_id=d, prerequisite_id=a)])
            await db.commit()
            return ids

    async def test_prerequisite_graph_serializes_disjoint_writers_and_rejects_cycle(self):
        a, b, c, d = await self.prerequisite_cycle_fixture()
        first_validated = asyncio.Event()
        release_first = asyncio.Event()
        async def paused_validation(db, **arguments):
            await _validate_course_prerequisite_replace(db, **arguments)
            if arguments["course_id"] == a:
                first_validated.set()
                await release_first.wait()
        async with self.session() as first_db, self.session() as second_db, self.session() as observer:
            first_pid = await first_db.scalar(text("SELECT pg_backend_pid()"))
            second_pid = await second_db.scalar(text("SELECT pg_backend_pid()"))
            async def replace(db, source, target, actor):
                try:
                    await replace_course_prerequisites(source, CoursePrerequisiteReplace(prerequisite_ids=[target]),
                        db=db, actor_id=actor, _role="admin")
                    return 200
                except HTTPException as error:
                    await db.rollback()
                    return error.status_code
            with patch("app.api.courses._validate_course_prerequisite_replace", side_effect=paused_validation):
                first = asyncio.create_task(replace(first_db, a, b, self.author))
                await asyncio.wait_for(first_validated.wait(), 3)
                second = asyncio.create_task(replace(second_db, c, d, self.reviewer))
                try:
                    await self.wait_until_backend_is_blocked(observer, second_pid, first_pid)
                    # A graph waiter must not hold a source row before the advisory lock.
                    await observer.execute(select(Course.id).where(Course.id == c).with_for_update(nowait=True))
                    await observer.rollback()
                    release_first.set()
                    statuses = await asyncio.wait_for(asyncio.gather(first, second), 5)
                    self.assertEqual(statuses, [200, 409])
                finally:
                    release_first.set()
                    for task in (first, second):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(first, second, return_exceptions=True)
        async with self.session() as db:
            edges = (await db.execute(select(CoursePrerequisite.course_id, CoursePrerequisite.prerequisite_id))).all()
            self.assertEqual(len(edges), 3)
            self.assertFalse(would_create_course_prerequisite_cycle(course_id=a, prerequisite_ids=[b],
                existing_edges=[edge for edge in edges if edge.course_id != a]))
            self.assertEqual(await db.scalar(select(func.count()).select_from(CourseAuditEvent)
                .where(CourseAuditEvent.action == "course.prerequisites.replace")), 1)

    async def test_prerequisite_target_visibility_is_revalidated_after_row_wait(self):
        a, b, _c, _d = await self.prerequisite_cycle_fixture()
        async with self.session() as hider, self.session() as writer, self.session() as observer:
            target = await hider.scalar(select(Course).where(Course.id == b).with_for_update())
            hider_pid = await hider.scalar(text("SELECT pg_backend_pid()"))
            writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
            async def replace():
                try:
                    await replace_course_prerequisites(a, CoursePrerequisiteReplace(prerequisite_ids=[b]),
                        db=writer, actor_id=self.author, _role="admin")
                    return 200
                except HTTPException as error:
                    await writer.rollback()
                    return error.status_code
            task = asyncio.create_task(replace())
            try:
                await self.wait_until_backend_is_blocked(observer, writer_pid, hider_pid)
                target.is_published = False
                await hider.commit()
                self.assertEqual(await asyncio.wait_for(task, 5), 409)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        async with self.session() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(CoursePrerequisite)
                .where(CoursePrerequisite.course_id == a)), 0)

    async def test_prerequisite_locking_preserves_validation_and_empty_replace(self):
        a, b, c, d = await self.prerequisite_cycle_fixture()
        async with self.session() as db:
            private = await db.get(Course, c)
            private.is_published = False
            await db.commit()
        for targets, expected in (([a], 422), ([uuid4()], 404), ([c], 409)):
            async with self.session() as db:
                with self.assertRaises(HTTPException) as caught:
                    await replace_course_prerequisites(a, CoursePrerequisiteReplace(prerequisite_ids=targets),
                        db=db, actor_id=self.author, _role="admin")
                self.assertEqual(caught.exception.status_code, expected)
                await db.rollback()
        async with self.session() as db:
            response = await replace_course_prerequisites(d, CoursePrerequisiteReplace(prerequisite_ids=[]),
                db=db, actor_id=self.author, _role="admin")
            self.assertEqual(response.prerequisite_ids, [])
            self.assertIsNone(await db.scalar(select(CoursePrerequisite).where(CoursePrerequisite.course_id == d)))

    async def test_tag_approval_revalidates_public_source_after_parent_lock_wait(self):
        course_id, lesson_id, _tags = await self.course_lesson_lock_fixture()
        for entity_type, source_id in (("course", course_id), ("lesson", lesson_id)):
            with self.subTest(entity_type=entity_type):
                async with self.session() as setup:
                    course = await setup.get(Course, course_id)
                    course.is_published = True
                    tag = KnowledgeTag(name="First hidden " + entity_type, slug="first-hidden-" + entity_type)
                    setup.add(tag)
                    await setup.commit()
                    proposal = await create_knowledge_proposal(KnowledgeRelationProposalCreate(
                        source={"type": entity_type, "id": str(source_id)},
                        target={"type": "knowledge_tag", "id": str(tag.id)}, relation_type="tag_affinity",
                        origin="rule", evidence_summary="공개 상태 재검증",
                        evidence=[{"evidence_kind": "rule", "summary": "첫 taxonomy 독립 검토"}]),
                        db=setup, actor_id=self.author, _role="admin")
                    proposal_id, tag_id = proposal.id, tag.id
                async with self.session() as hider, self.session() as reviewer, self.session() as observer:
                    course = await hider.scalar(select(Course).where(Course.id == course_id).with_for_update())
                    hider_pid = await hider.scalar(text("SELECT pg_backend_pid()"))
                    reviewer_pid = await reviewer.scalar(text("SELECT pg_backend_pid()"))
                    async def approve():
                        try:
                            await review_knowledge_proposal(proposal_id,
                                KnowledgeProposalReviewRequest(action="approve", reason="독립 승인 중 공개 경합"),
                                db=reviewer, actor_id=self.reviewer, _role="admin")
                            return 200
                        except HTTPException as error:
                            await reviewer.rollback()
                            return error.status_code
                    task = asyncio.create_task(approve())
                    try:
                        await self.wait_until_backend_is_blocked(observer, reviewer_pid, hider_pid)
                        course.is_published = False
                        await hider.commit()
                        self.assertEqual(await asyncio.wait_for(task, 5), 409)
                    finally:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                async with self.session() as reader:
                    proposal = await reader.get(KnowledgeRelationProposal, proposal_id)
                    self.assertEqual(proposal.state, "pending_review")
                    self.assertIsNone(proposal.published_relation_id)
                    model = CourseKnowledgeTag if entity_type == "course" else LessonKnowledgeTag
                    self.assertIsNone(await reader.get(model, (source_id, tag_id)))


if __name__ == "__main__":
    outcome = unittest.main(verbosity=2, exit=False).result
    print(json.dumps({"ok": outcome.wasSuccessful(), "tests": outcome.testsRun,
                      "failures": len(outcome.failures), "errors": len(outcome.errors), "skipped": len(outcome.skipped)}))
    raise SystemExit(not outcome.wasSuccessful())
