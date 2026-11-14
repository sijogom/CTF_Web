"""명시된 작업 DB의 UUID schema에서만 삭제·잠금·migration을 인수한다."""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

from app.api.knowledge_contents import search_knowledge_contents
from app.api.knowledge_tags import delete_tag, list_mapping_events
from app.models.course import Base, Course, Lesson
import app.models.education
import app.models.event
import app.models.challenge_submission
import app.models.lesson_image
import app.models.challenge_image_cleanup
import app.models.challenge_attachment
from app.models.challenge import Challenge
from app.models.knowledge import (
    ChallengeKnowledgeTag, KnowledgeContentNode, KnowledgeGraphRevision, KnowledgeProjectionOutbox,
    KnowledgeRelation, KnowledgeTag, KnowledgeTagDeletionArchive, KnowledgeTagMappingAuditEvent,
)
from app.schemas.knowledge import ContentTagMappingUpdate
from app.utils.content_tag_mappings import replace_content_tag_mappings
from app.utils.knowledge_relations import ResolvedKnowledgeContent, ensure_content_node
from app.utils.knowledge_tag_deletion import delete_tag_preserving_history, load_tag_deletion_impact
from app.utils.knowledge_tag_projection import project_rule_tag_mappings


DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")


def install_migration(connection, filename):
    spec = importlib.util.spec_from_file_location("knowledge_management_migration", SERVICE / "migrations/versions" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        module.upgrade()


@unittest.skipUnless(DATABASE_URL, "명시된 KNOWLEDGE_TEST_DATABASE_URL 작업 DB가 필요합니다.")
class KnowledgeManagementPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "knowledge_management_" + uuid4().hex
        self.admin_engine = create_async_engine(DATABASE_URL)
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_async_engine(DATABASE_URL, connect_args={"server_settings": {
            "search_path": self.schema, "statement_timeout": "10000"}}, pool_size=5)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(install_migration, "20261013_028_knowledge_mapping_ownership_revision.py")
        self.actor = uuid4()
        async with self.session() as db:
            tag = KnowledgeTag(name="삭제 태그", slug="deletion-tag")
            challenge = Challenge(name="보존 문제", category="web", challenge_type="wargame", is_published=True)
            db.add_all([tag, challenge])
            await db.flush()
            self.tag_id, self.challenge_id = tag.id, challenge.id
            db.add(ChallengeKnowledgeTag(challenge_id=challenge.id, tag_id=tag.id,
                origin="manual", created_by=self.actor, evidence_summary="원래 근거"))
            db.add(KnowledgeTagMappingAuditEvent(entity_type="challenge", entity_id=str(challenge.id),
                tag_id=tag.id, action="attach", actor_id=self.actor, mapping_snapshot={"original": "보존"}))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        await self.admin_engine.dispose()

    def session(self):
        return AsyncSession(self.engine, expire_on_commit=False)

    async def assert_database_lock_wait(self, backend_pid, pending):
        async def observe():
            async with self.engine.connect() as connection:
                while True:
                    self.assertFalse(pending.done(), "writer가 DB 잠금 대기 전에 종료되었습니다.")
                    wait = await connection.scalar(text(
                        "SELECT wait_event_type FROM pg_catalog.pg_stat_activity WHERE pid=:pid"), {"pid": backend_pid})
                    if wait == "Lock":
                        return
                    await connection.rollback()
                    await asyncio.sleep(0.02)
        await asyncio.wait_for(observe(), 3)

    async def test_delete_preserves_audit_updates_revision_and_rules_do_not_recreate_tag(self):
        from app.api.knowledge_tags import reconcile_rule_mappings
        async with self.session() as db:
            before_revision = await db.scalar(select(func.count()).select_from(KnowledgeGraphRevision))
            preview = (await load_tag_deletion_impact(db, self.tag_id)).preview()
            await delete_tag(self.tag_id, db=db, _="admin", actor_id=self.actor, confirmation=preview.confirmation)
            self.assertIsNone(await db.get(KnowledgeTag, self.tag_id))
            self.assertIsNotNone(await db.get(Challenge, self.challenge_id))
            audit = await list_mapping_events("challenge", str(self.challenge_id), db=db, _="admin")
            self.assertEqual(len(audit), 2)
            self.assertTrue(any(row.mapping_snapshot == {"original": "보존"} for row in audit))
            self.assertEqual(await db.scalar(select(func.count()).select_from(KnowledgeTagDeletionArchive)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(KnowledgeGraphRevision)), before_revision + 1)
            await reconcile_rule_mappings(dry_run=False, db=db, actor_id=self.actor, _role="admin")
            self.assertIsNone(await db.get(KnowledgeTag, self.tag_id))

    async def test_mapping_writer_conflict_is_409_and_preserves_all_records(self):
        async with self.session() as writer, self.session() as deleting:
            await writer.scalar(select(ChallengeKnowledgeTag).where(
                ChallengeKnowledgeTag.challenge_id == self.challenge_id, ChallengeKnowledgeTag.tag_id == self.tag_id).with_for_update())
            with self.assertRaises(HTTPException) as raised:
                await delete_tag(self.tag_id, db=deleting, _="admin", actor_id=self.actor, confirmation=None)
            self.assertEqual(raised.exception.status_code, 409)
            self.assertIsNotNone(await deleting.get(KnowledgeTag, self.tag_id))
            self.assertEqual(await deleting.scalar(select(func.count()).select_from(KnowledgeTagDeletionArchive)), 0)
            self.assertEqual(await deleting.scalar(select(func.count()).select_from(KnowledgeTagMappingAuditEvent)), 1)
            await writer.rollback()

    async def test_late_mapping_writer_cannot_attach_deleted_tag(self):
        async with self.session() as deleting:
            await delete_tag_preserving_history(deleting, self.tag_id, actor_id=self.actor)
            backend_pid = asyncio.get_running_loop().create_future()
            async def writer():
                async with self.session() as db:
                    backend_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
                    challenge = await db.get(Challenge, self.challenge_id)
                    try:
                        await replace_content_tag_mappings(db, entity_type="challenge", entity=challenge,
                            body=ContentTagMappingUpdate(tag_ids=[self.tag_id]), actor_id=self.actor,
                            synchronize_rules=False)
                    except HTTPException as exc:
                        await db.rollback()
                        return exc.status_code
                    await db.commit()
                    return 200
            pending = asyncio.create_task(writer())
            try:
                pid = await asyncio.wait_for(backend_pid, 3)
                await self.assert_database_lock_wait(pid, pending)
                await deleting.commit()
                self.assertEqual(await asyncio.wait_for(pending, 5), 404)
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        async with self.session() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ChallengeKnowledgeTag)), 0)

    async def test_late_content_node_upsert_cannot_resurrect_deleted_tag(self):
        stale = ResolvedKnowledgeContent(entity_type="knowledge_tag", entity_id=str(self.tag_id),
            display_name="삭제 태그", is_active=True, href="/learn")
        async with self.session() as deleting:
            await delete_tag_preserving_history(deleting, self.tag_id, actor_id=self.actor)
            backend_pid = asyncio.get_running_loop().create_future()
            async def writer():
                async with self.session() as db:
                    backend_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
                    try:
                        await ensure_content_node(db, stale)
                    except HTTPException as exc:
                        await db.rollback()
                        return exc.status_code
                    await db.commit()
                    return 200
            pending = asyncio.create_task(writer())
            try:
                pid = await asyncio.wait_for(backend_pid, 3)
                await self.assert_database_lock_wait(pid, pending)
                await deleting.commit()
                self.assertEqual(await asyncio.wait_for(pending, 5), 409)
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        async with self.session() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(KnowledgeContentNode)), 0)

    async def test_new_mapping_validation_waits_then_rejects_deleted_tag(self):
        async with self.session() as db:
            challenge = Challenge(name="아직 매핑 없는 문제", category="web", challenge_type="wargame", is_published=True)
            db.add(challenge)
            await db.commit()
            target_id = challenge.id
        async with self.session() as deleting:
            await delete_tag_preserving_history(deleting, self.tag_id, actor_id=self.actor)
            backend_pid = asyncio.get_running_loop().create_future()
            async def writer():
                async with self.session() as db:
                    backend_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
                    entity = await db.get(Challenge, target_id)
                    try:
                        await replace_content_tag_mappings(db, entity_type="challenge", entity=entity,
                            body=ContentTagMappingUpdate(tag_ids=[self.tag_id]), actor_id=self.actor, synchronize_rules=False)
                    except HTTPException as exc:
                        await db.rollback()
                        return exc.status_code
                    await db.commit()
                    return 200
            pending = asyncio.create_task(writer())
            try:
                await self.assert_database_lock_wait(await asyncio.wait_for(backend_pid, 3), pending)
                await deleting.commit()
                self.assertEqual(await asyncio.wait_for(pending, 5), 404)
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        async with self.session() as db:
            self.assertIsNotNone(await db.get(Challenge, target_id))
            self.assertEqual(await db.scalar(select(func.count()).select_from(ChallengeKnowledgeTag)), 0)

    async def test_rule_projection_skips_tag_deleted_after_slug_snapshot(self):
        async with self.session() as db:
            challenge = Challenge(name="규칙 매핑 문제", category="web", tags=["deletion-tag"], challenge_type="wargame", is_published=True)
            db.add(challenge)
            await db.commit()
            target_id = challenge.id
        async with self.session() as deleting:
            await delete_tag_preserving_history(deleting, self.tag_id, actor_id=self.actor)
            backend_pid = asyncio.get_running_loop().create_future()
            async def writer():
                async with self.session() as db:
                    backend_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
                    entity = await db.get(Challenge, target_id)
                    created = await project_rule_tag_mappings(db, entity_type="challenge", entity=entity, actor_id=self.actor)
                    await db.commit()
                    return created
            pending = asyncio.create_task(writer())
            try:
                await self.assert_database_lock_wait(await asyncio.wait_for(backend_pid, 3), pending)
                await deleting.commit()
                self.assertEqual(await asyncio.wait_for(pending, 5), 0)
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        async with self.session() as db:
            self.assertIsNotNone(await db.get(Challenge, target_id))
            self.assertIsNone(await db.get(KnowledgeTag, self.tag_id))
            self.assertEqual(await db.scalar(select(func.count()).select_from(ChallengeKnowledgeTag)), 0)

    async def test_approved_relation_review_lock_makes_delete_409_and_rolls_back(self):
        async with self.session() as db:
            source = KnowledgeContentNode(entity_type="challenge", entity_id=str(self.challenge_id), display_name="보존 문제", is_active=True)
            target = KnowledgeContentNode(entity_type="knowledge_tag", entity_id=str(self.tag_id), display_name="삭제 태그", is_active=True)
            db.add_all([source, target])
            await db.flush()
            relation = KnowledgeRelation(source_node_id=source.id, target_node_id=target.id,
                relation_type="tag_affinity", created_by=self.actor, status="approved", strength=0.8, confidence=0.9)
            db.add(relation)
            await db.commit()
            relation_id, target_id = relation.id, target.id
        async with self.session() as reviewing, self.session() as deleting:
            await reviewing.scalar(select(KnowledgeRelation).where(KnowledgeRelation.id == relation_id).with_for_update())
            with self.assertRaises(HTTPException) as raised:
                await delete_tag(self.tag_id, db=deleting, _="admin", actor_id=self.actor, confirmation=None)
            self.assertEqual(raised.exception.status_code, 409)
            self.assertIsNotNone(await deleting.get(KnowledgeTag, self.tag_id))
            self.assertTrue((await deleting.get(KnowledgeContentNode, target_id)).is_active)
            self.assertEqual((await deleting.get(KnowledgeRelation, relation_id)).status, "approved")
            self.assertEqual(await deleting.scalar(select(func.count()).select_from(KnowledgeTagDeletionArchive)), 0)
            self.assertEqual(await deleting.scalar(select(func.count()).select_from(KnowledgeProjectionOutbox)), 0)
            await reviewing.rollback()

    async def test_failure_rolls_back_tag_junction_archive_and_revision(self):
        async with self.session() as db:
            before = await db.scalar(select(func.count()).select_from(KnowledgeGraphRevision))
            with patch.object(db, "flush", new=AsyncMock(side_effect=RuntimeError("rollback proof"))):
                with self.assertRaisesRegex(RuntimeError, "rollback proof"):
                    await delete_tag(self.tag_id, db=db, _="admin", actor_id=self.actor, confirmation=None)
            self.assertIsNotNone(await db.get(KnowledgeTag, self.tag_id))
            self.assertEqual(await db.scalar(select(func.count()).select_from(ChallengeKnowledgeTag)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(KnowledgeTagDeletionArchive)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(KnowledgeProjectionOutbox)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(KnowledgeGraphRevision)), before)

    async def test_search_paginates_private_uuid_titles_and_large_numeric_query(self):
        async with self.session() as db:
            courses = [Course(title="중복 제목", category="web", difficulty="basic", author_id=self.actor) for _ in range(3)]
            db.add_all(courses)
            await db.commit()
            response = await search_knowledge_contents("course", "중복", 1, 1, db, self.actor, "admin")
            expected = sorted(str(row.id) for row in courses)[1]
            self.assertEqual(response.total, 3)
            self.assertEqual(response.items[0].id, expected)
            self.assertFalse(response.items[0].is_active)
            exact = await search_knowledge_contents("course", str(courses[0].id), 0, 20, db, self.actor, "admin")
            self.assertEqual([row.id for row in exact.items], [str(courses[0].id)])
            huge = await search_knowledge_contents("challenge", "9" * 200, 0, 20, db, self.actor, "admin")
            self.assertEqual(huge.total, 0)

    async def test_migration_032_keeps_existing_ledger_bytes_and_supports_physical_delete(self):
        async with self.engine.begin() as connection:
            await connection.execute(text("DROP TABLE knowledge_tag_deletion_archives"))
            await connection.execute(text("ALTER TABLE knowledge_tag_mapping_audit_events ADD CONSTRAINT fk_knowledge_tag_mapping_audit_tag FOREIGN KEY(tag_id) REFERENCES knowledge_tags(id) ON DELETE RESTRICT"))
            before = (await connection.execute(text("SELECT id, tag_id, mapping_snapshot::text, actor_id, created_at FROM knowledge_tag_mapping_audit_events ORDER BY id"))).all()
            await connection.run_sync(install_migration, "20261020_032_knowledge_tag_deletion_archive.py")
            after = (await connection.execute(text("SELECT id, tag_id, mapping_snapshot::text, actor_id, created_at FROM knowledge_tag_mapping_audit_events ORDER BY id"))).all()
            self.assertEqual(before, after)
        async with self.session() as db:
            await delete_tag(self.tag_id, db=db, _="admin", actor_id=self.actor, confirmation=None)
            self.assertIsNone(await db.get(KnowledgeTag, self.tag_id))
            self.assertEqual(await db.scalar(select(func.count()).select_from(KnowledgeTagMappingAuditEvent)), 2)


if __name__ == "__main__":
    unittest.main()
