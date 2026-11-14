"""AP1의 실제 PG16 경계 7개. MinIO SDK는 명시적으로 fake이며 실물 인수가 아니다.

CHALLENGE_ATTACHMENT_TEST_DATABASE_URL로 지정한 ctfi_l_UUID DB/동명 role만
사용한다. 모든 DDL/DML은 매 검사에서 만든 UUID schema 안에서 수행한다.
030은 현재 metadata에서 AP1 추가분만 제외한 old-column projection이며,
실제 031 upgrade와 구 column ORM CRUD를 검사하되 과거 image 전체를 인수하지 않는다.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

SERVICE_ROOT = Path(__file__).resolve().parents[1]
for path in (SERVICE_ROOT, SERVICE_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import BackgroundTasks, HTTPException, UploadFile
from sqlalchemy import MetaData, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import registry

from test_challenge_import_postgres import validate_work_database_url
from app.api.challenges import update_challenge
from app.api.submissions import approve_submission
from app.models.course import Base
from app.models.challenge import Challenge, Flag, Hint, Submission
from app.models.challenge_attachment import ChallengeAttachment, ChallengeAttachmentDraft
from app.models.challenge_submission import ChallengeSubmission
from app.models.knowledge import ChallengeKnowledgeTag, KnowledgeTag, KnowledgeTagMappingAuditEvent
from app.schemas.challenge import ChallengeUpdate
from app.schemas.challenge_attachment import AttachmentDraftRequest
from app.schemas.knowledge import ContentTagMappingItem, ContentTagMappingUpdate
from app.services import challenge_attachments as attachments
from app.services.challenge_imports import import_challenge_batch
from app.utils.content_tag_mappings import get_content_tag_mapping


DATABASE_URL = os.environ.get("CHALLENGE_ATTACHMENT_TEST_DATABASE_URL")
SCHEMA_PATTERN = re.compile(r"challenge_attachment_test_[0-9a-f]{32}\Z")
MIGRATION_PATH = SERVICE_ROOT / "migrations/versions/20261018_031_challenge_attachments.py"


def memory_upload(data=b"AP1 bytes", name="원본 자료.bin"):
    stream = io.BytesIO(data)
    stream._rolled = False
    return UploadFile(stream, filename=name)


def migration_031():
    spec = importlib.util.spec_from_file_location("ap1_pg031", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def metadata_030_projection():
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name not in {"challenge_attachment_drafts", "challenge_attachments"}:
            table.to_metadata(metadata)
    # 신규 column만 제외한다. 기존 column/default/constraint를 새 정책으로 만들지 않는다.
    table = metadata.tables["challenges"]
    table._columns.remove(table.c.attachment_revision)
    return metadata


@unittest.skipUnless(DATABASE_URL, "명시적인 CHALLENGE_ATTACHMENT_TEST_DATABASE_URL 작업 DB가 필요합니다.")
class ChallengeAttachmentsPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.url = validate_work_database_url(DATABASE_URL)
        self.schemas, self.engines, self.tasks = [], [], []
        self.sdk_patch = None
        settings = {"statement_timeout": "15000", "lock_timeout": "4000",
                    "application_name": "challenge_attachment_test"}
        self.admin_engine = create_async_engine(DATABASE_URL, echo=False, hide_parameters=True,
            pool_size=1, max_overflow=0, pool_timeout=4, connect_args={"server_settings": settings})
        self.addAsyncCleanup(self.cleanup_owned_schema)
        async with self.admin_engine.connect() as connection:
            identity = (await connection.execute(text(
                "SELECT current_database(),current_user,host(inet_server_addr()),inet_server_port(),"
                "current_setting('server_version_num')::int,d.datdba=r.oid,r.rolsuper,r.rolbypassrls "
                "FROM pg_database d JOIN pg_roles r ON r.rolname=current_user "
                "WHERE d.datname=current_database()"
            ))).one()
            self.assertEqual(tuple(identity[:4]), (self.url.database, self.url.username, "127.0.0.1", 15433))
            self.assertTrue(160000 <= identity[4] < 170000)
            self.assertEqual(tuple(identity[5:]), (True, False, False))
        self.schema = "challenge_attachment_test_" + uuid4().hex
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
            row = (await connection.execute(text(
                "SELECT n.oid,r.rolname FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner "
                "WHERE n.nspname=:name"), {"name": self.schema})).one()
            self.assertEqual(row[1], self.url.username)
            self.schemas.append((self.schema, row[0], row[1]))
        settings = {**settings, "search_path": self.schema}
        self.engine = create_async_engine(DATABASE_URL, echo=False, hide_parameters=True,
            pool_size=5, max_overflow=0, pool_timeout=4, connect_args={"server_settings": settings})
        self.engines.append(self.engine)
        async with self.engine.begin() as connection:
            self.assertEqual(await connection.scalar(text("SELECT current_schema()")), self.schema)
            self.assertEqual(await connection.scalar(text("SHOW search_path")), self.schema)
            metadata = metadata_030_projection() if self._testMethodName.startswith("test_030_") else Base.metadata
            await connection.run_sync(metadata.create_all)
        self.owner = uuid4()
        self.fake_objects = {}
        self.sdk_patch = patch.object(attachments, "run_in_threadpool", self.fake_sdk)
        self.sdk_patch.start()

    async def fake_sdk(self, fn, *args):
        if fn is attachments.ensure_challenge_private_bucket:
            return None
        if fn is attachments.stat_challenge_object:
            data = self.fake_objects.get(args[0])
            return SimpleNamespace(size=len(data)) if data is not None else None
        if fn is attachments.verify_challenge_object:
            path, size, digest = args
            data = self.fake_objects.get(path)
            if data is None:
                return "missing"
            self.assertEqual((len(data), hashlib.sha256(data).hexdigest()), (size, digest))
            return "verified"
        raise AssertionError("MinIO SDK fake: unexpected operation")

    async def cleanup_owned_schema(self):
        try:
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            if self.tasks:
                await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 6)
            for engine in self.engines:
                await engine.dispose()
            if self.schemas:
                async with self.admin_engine.begin() as connection:
                    for name, oid, owner in self.schemas:
                        self.assertRegex(name, SCHEMA_PATTERN)
                        current = (await connection.execute(text(
                            "SELECT n.oid,r.rolname FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner "
                            "WHERE n.nspname=:name"), {"name": name})).one_or_none()
                        if current is None:
                            continue
                        # 재사용되거나 다른 owner인 namespace는 지우지 않는다.
                        self.assertEqual(tuple(current), (oid, owner))
                        self.assertEqual(owner, self.url.username)
                        await connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        finally:
            try:
                await self.admin_engine.dispose()
            finally:
                # 종료 불명확 task에 실제 SDK를 되돌려 외부 송신을 허용하지 않는다.
                if self.sdk_patch is not None and all(task.done() for task in self.tasks):
                    self.sdk_patch.stop()

    def session(self):
        return AsyncSession(self.engine, expire_on_commit=False)

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def parent(self, *, name=None, files=None):
        async with self.session() as db:
            row = Challenge(name=name or "ap1-" + uuid4().hex, category="misc", difficulty="easy",
                description="<p>Before</p>", flag_value="ap1-owned-fixture", files=files or [], author_id=self.owner)
            db.add(row)
            await db.commit()
            return row.id

    async def draft(self, challenge_id):
        identifier = uuid4()
        async with self.session() as db:
            response = await attachments.create_draft(db, AttachmentDraftRequest(
                draft_id=identifier, challenge_id=challenge_id), self.owner)
            self.assertEqual(response.state, "open")
        return identifier

    async def asset(self, draft_id, *, challenge_id=None, data=b"AP1 bytes", position=0, path=None):
        identifier = uuid4()
        async with self.session() as db:
            row = ChallengeAttachment(id=identifier, owner_id=self.owner, draft_id=draft_id,
                original_name="원본 자료.bin", byte_size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                content_type="application/octet-stream", state="ready" if path is None else "pending",
                object_path=path or attachments.private_path(self.owner, draft_id, identifier),
                challenge_id=challenge_id, bound_challenge_id=challenge_id, position=position)
            db.add(row)
            await db.commit()
            self.fake_objects[row.object_path] = data
        return identifier

    async def locked_parent(self, db, identifier):
        return (await db.execute(select(Challenge).where(Challenge.id == identifier).with_for_update()
                                 .execution_options(populate_existing=True))).scalar_one()

    async def test_030_projection_upgrade_old_column_crud_default_and_downgrade_refusal(self):
        metadata = metadata_030_projection()
        mapper = registry(metadata=metadata)
        class OldColumnChallenge:
            pass
        mapper.map_imperatively(OldColumnChallenge, metadata.tables["challenges"])
        try:
            async with self.session() as db:
                old = OldColumnChallenge()
                old.name, old.category, old.flag_value = "ap1-old-" + uuid4().hex, "misc", "ap1-owned"
                old.files = ["실물 없는 기존 이름.bin", "challenges/misc/easy/17/legacy.bin"]
                db.add(old)
                await db.commit()
                identifier = old.id
                before = (await db.execute(text("SELECT to_jsonb(c) FROM challenges c WHERE id=:id"), {"id": identifier})).scalar_one()
            migration = migration_031()
            self.assertEqual((migration.down_revision, migration.revision), ("20261018_030", "20261018_031"))
            async with self.engine.begin() as connection:
                def upgrade(sync_connection):
                    with Operations.context(MigrationContext.configure(sync_connection)):
                        migration.upgrade()
                await connection.run_sync(upgrade)
            async with self.session() as db:
                after = (await db.execute(text("SELECT to_jsonb(c) FROM challenges c WHERE id=:id"), {"id": identifier})).scalar_one()
                self.assertEqual(after.pop("attachment_revision"), 0)
                self.assertEqual(after, before)
                row = await db.get(OldColumnChallenge, identifier)
                self.assertEqual(row.files, before["files"])
                row.description = "<p>Old-column update</p>"
                await db.commit()
                await db.refresh(row)
                self.assertEqual(row.description, "<p>Old-column update</p>")
                extra = OldColumnChallenge()
                extra.name, extra.category, extra.flag_value = "ap1-old-delete-" + uuid4().hex, "misc", "ap1-owned"
                db.add(extra)
                await db.flush()
                extra_id = extra.id
                self.assertIsNotNone(await db.get(OldColumnChallenge, extra_id))
                await db.delete(extra)
                await db.flush()
                self.assertIsNone(await db.get(OldColumnChallenge, extra_id))
                await db.commit()
                self.assertEqual((await db.get(Challenge, identifier)).attachment_revision, 0)
            draft_id = await self.draft(identifier)
            asset_id = await self.asset(draft_id)
            async with self.session() as db:
                ledger_before = (
                    await db.scalar(text("SELECT to_jsonb(d) FROM challenge_attachment_drafts d WHERE id=:id"), {"id": draft_id}),
                    await db.scalar(text("SELECT to_jsonb(a) FROM challenge_attachments a WHERE id=:id"), {"id": asset_id}),
                )
            async with self.engine.begin() as connection:
                def downgrade(sync_connection):
                    with Operations.context(MigrationContext.configure(sync_connection)):
                        migration.downgrade()
                with self.assertRaises(RuntimeError):
                    await connection.run_sync(downgrade)
            async with self.session() as db:
                self.assertIsNotNone(await db.get(ChallengeAttachmentDraft, draft_id))
                self.assertIsNotNone(await db.get(ChallengeAttachment, asset_id))
                self.assertEqual((await db.get(Challenge, identifier)).files, before["files"])
                self.assertEqual((
                    await db.scalar(text("SELECT to_jsonb(d) FROM challenge_attachment_drafts d WHERE id=:id"), {"id": draft_id}),
                    await db.scalar(text("SELECT to_jsonb(a) FROM challenge_attachments a WHERE id=:id"), {"id": asset_id}),
                ), ledger_before)
        finally:
            mapper.dispose()

    async def test_two_drafts_concurrent_cas_and_stale_same_result_idempotence(self):
        parent_id = await self.parent()
        draft_ids = [await self.draft(parent_id) for _ in range(2)]
        asset_ids = [await self.asset(identifier) for identifier in draft_ids]
        ready, start = [asyncio.Event(), asyncio.Event()], asyncio.Event()
        async def writer(index):
            async with self.session() as db:
                ready[index].set()
                await asyncio.wait_for(start.wait(), 5)
                row = await self.locked_parent(db, parent_id)
                try:
                    await attachments.bind_attachments(db, row, actor_id=self.owner,
                        attachment_ids=[asset_ids[index]], attachment_draft_id=draft_ids[index], previous_files=[])
                    await db.commit()
                    return "committed", index
                except HTTPException as error:
                    await db.rollback()
                    self.assertEqual((error.status_code, error.detail["code"]), (409, "attachment_revision_conflict"))
                    return "conflict", index
        with patch.object(attachments, "observe_upload", AsyncMock(return_value="verified")):
            tasks = [self.task(writer(index)) for index in range(2)]
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ready)), 5)
            start.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), 12)
        self.assertEqual(sorted(status for status, _ in results), ["committed", "conflict"])
        winner = next(index for status, index in results if status == "committed")
        loser = 1 - winner
        async with self.session() as db:
            row = await self.locked_parent(db, parent_id)
            self.assertEqual(row.attachment_revision, 1)
            await attachments.bind_attachments(db, row, actor_id=self.owner,
                attachment_ids=[asset_ids[winner]], attachment_draft_id=draft_ids[loser], previous_files=[])
            await db.commit()
            self.assertEqual(row.attachment_revision, 1)
        stale_draft = await self.draft(parent_id)
        async with self.session() as db:
            row = await self.locked_parent(db, parent_id)
            row.files = ["legacy edit.bin"]
            await attachments.bind_attachments(db, row, actor_id=self.owner, previous_files=[])
            await db.commit()
            self.assertEqual(row.attachment_revision, 2)
        async with self.session() as db:
            row = await self.locked_parent(db, parent_id)
            row.files = []
            with self.assertRaises(HTTPException) as caught:
                await attachments.bind_attachments(db, row, actor_id=self.owner, attachment_draft_id=stale_draft,
                    attachment_ids=[asset_ids[winner]], previous_files=["legacy edit.bin"])
            self.assertEqual(caught.exception.detail["code"], "attachment_revision_conflict")
            await db.rollback()
        async with self.session() as db:
            self.assertEqual((await db.get(Challenge, parent_id)).files, ["legacy edit.bin"])
            rows = (await db.scalars(select(ChallengeAttachment).order_by(ChallengeAttachment.id))).all()
            self.assertEqual({row.id for row in rows}, set(asset_ids))
            self.assertEqual([row.id for row in rows if row.challenge_id == parent_id], [asset_ids[winner]])

    async def test_parent_mapping_and_attachment_failure_share_one_rollback(self):
        parent_id, other_id = await self.parent(), await self.parent()
        draft_id, foreign_draft = await self.draft(parent_id), await self.draft(other_id)
        foreign_id = await self.asset(foreign_draft, challenge_id=other_id)
        async with self.session() as db:
            tag = KnowledgeTag(name="AP1 tag", slug="ap1-" + uuid4().hex)
            db.add(tag)
            await db.flush()
            db.add(ChallengeKnowledgeTag(challenge_id=parent_id, tag_id=tag.id, origin="manual",
                created_by=self.owner, strength=0.4, confidence=0.8, evidence_summary="Before"))
            await db.commit()
            tag_id = tag.id
            mapping = await get_content_tag_mapping(db, entity_type="challenge", entity_id=parent_id)
        async with self.session() as db:
            with self.assertRaises(HTTPException) as caught:
                await update_challenge(parent_id, ChallengeUpdate(name="Must rollback", attachment_draft_id=draft_id,
                    attachment_ids=[foreign_id], knowledge_tag_mapping=ContentTagMappingUpdate(
                        expected_revision=mapping.mapping_revision,
                        mappings=[ContentTagMappingItem(tag_id=tag_id, strength=0.9, confidence=0.9, evidence_summary="Must rollback")])),
                    BackgroundTasks(), db=db, actor_id=self.owner, _role="admin")
            self.assertEqual(caught.exception.detail["code"], "attachment_reference_conflict")
            await db.rollback()
        async with self.session() as db:
            row = await db.get(Challenge, parent_id)
            self.assertNotEqual(row.name, "Must rollback")
            self.assertEqual((row.description, row.attachment_revision), ("<p>Before</p>", 0))
            mapping = await db.get(ChallengeKnowledgeTag, (parent_id, tag_id))
            self.assertEqual((float(mapping.strength), mapping.evidence_summary), (0.4, "Before"))
            self.assertEqual(await db.scalar(select(func.count()).select_from(KnowledgeTagMappingAuditEvent)), 0)
            self.assertEqual((await db.get(ChallengeAttachment, foreign_id)).challenge_id, other_id)
            self.assertEqual((await db.get(ChallengeAttachmentDraft, draft_id)).state, "open")

    async def test_durable_reservation_active_get_and_same_uuid_physical_lock(self):
        parent_id = await self.parent()
        draft_id, upload_id, data = await self.draft(parent_id), uuid4(), b"AP1 real-PG fake-SDK bytes"
        entered, release = asyncio.Event(), asyncio.Event()
        objects, puts, pids = {}, [], []
        writer_task = None
        async with self.session() as writer_db:
            async def sdk(fn, *args):
                if asyncio.current_task() is writer_task:
                    pids.append(await writer_db.scalar(text("SELECT pg_backend_pid()")))
                if fn is attachments.ensure_challenge_private_bucket:
                    return None
                if fn is attachments.verify_challenge_object:
                    path, size, digest = args
                    if path not in objects:
                        return "missing"
                    self.assertEqual((len(objects[path]), hashlib.sha256(objects[path]).hexdigest()), (size, digest))
                    return "verified"
                self.assertIs(fn, attachments.put_private_challenge_object)
                path, stream, size, _content_type = args
                entered.set()
                await asyncio.wait_for(release.wait(), 8)
                stream.seek(0)
                objects[path] = stream.read()
                self.assertEqual(len(objects[path]), size)
                puts.append(path)
            with patch.object(attachments, "run_in_threadpool", sdk):
                writer_task = self.task(attachments.upload_attachment(writer_db, draft_id, upload_id,
                    self.owner, memory_upload(data)))
                try:
                    await asyncio.wait_for(entered.wait(), 8)
                    async with self.session() as observer:
                        asset = await observer.get(ChallengeAttachment, upload_id)
                        self.assertEqual((asset.state, asset.sha256), ("pending", hashlib.sha256(data).hexdigest()))
                        status = await attachments.get_upload_status(observer, draft_id, upload_id, self.owner)
                        self.assertEqual((status.state, status.retry_allowed), ("pending", False))
                    async with self.session() as duplicate:
                        result = await attachments.upload_attachment(duplicate, draft_id, upload_id,
                            self.owner, memory_upload(data))
                        self.assertEqual(result.state, "pending")
                    self.assertEqual(puts, [])
                finally:
                    release.set()
                self.assertTrue((await asyncio.wait_for(writer_task, 8)).ready_for_attach)
                self.assertEqual(len(set(pids)), 1)
                self.assertGreaterEqual(len(pids), 4)
                async with self.session() as observer:
                    before = await observer.scalar(text("SELECT xmin::text FROM challenge_attachments WHERE id=:id"), {"id": upload_id})
                    status = await attachments.get_upload_status(observer, draft_id, upload_id, self.owner)
                    after = await observer.scalar(text("SELECT xmin::text FROM challenge_attachments WHERE id=:id"), {"id": upload_id})
                    self.assertEqual(before, after)
                    self.assertTrue(status.ready_for_attach)
                    self.assertEqual(await observer.scalar(select(func.count()).select_from(ChallengeAttachment)), 1)
        self.assertEqual(len(puts), 1)

    async def test_closed_draft_after_reservation_refreshes_cached_open_and_puts_zero(self):
        parent_id = await self.parent()
        draft_id, upload_id = await self.draft(parent_id), uuid4()
        reserved, proceed = asyncio.Event(), asyncio.Event()
        real_lock = attachments.try_upload_lock
        async def paused_lock(db, identifier):
            reserved.set()
            await asyncio.wait_for(proceed.wait(), 8)
            return await real_lock(db, identifier)
        async with self.session() as writer:
            cached = await attachments.owned_draft(writer, draft_id, self.owner)
            with patch.object(attachments, "try_upload_lock", paused_lock), \
                 patch.object(attachments, "run_in_threadpool", AsyncMock(side_effect=AssertionError("closed draft accessed SDK"))) as sdk:
                task = self.task(attachments.upload_attachment(writer, draft_id, upload_id, self.owner, memory_upload()))
                try:
                    await asyncio.wait_for(reserved.wait(), 8)
                    async with self.session() as closer:
                        row = await self.locked_parent(closer, parent_id)
                        await attachments.bind_attachments(closer, row, actor_id=self.owner,
                            attachment_draft_id=draft_id, attachment_ids=[], previous_files=[])
                        await closer.commit()
                    self.assertEqual(cached.state, "open")
                finally:
                    proceed.set()
                with self.assertRaises(HTTPException) as caught:
                    await asyncio.wait_for(task, 8)
                self.assertEqual((caught.exception.status_code, caught.exception.detail["code"]), (409, "attachment_draft_closed"))
                self.assertEqual(cached.state, "bound")
                sdk.assert_not_awaited()
        async with self.session() as db:
            self.assertEqual((await db.get(ChallengeAttachment, upload_id)).state, "pending")
            self.assertEqual((await db.get(ChallengeAttachmentDraft, draft_id)).state, "bound")

    async def test_approval_second_copy_failure_rolls_back_pg_but_not_fake_object_copy(self):
        submission_id = uuid4()
        paths = [f"challenge-pending/misc/easy/{submission_id}/{index}.bin" for index in range(2)]
        async with self.session() as db:
            db.add(ChallengeSubmission(id=submission_id, submitter_id=self.owner, submitter_name="AP1 owned",
                name="ap1-approval-" + uuid4().hex, category="misc", difficulty="easy", description="<p>Before</p>",
                flag_type="static", flag_value="ap1-owned-fixture", file_paths=paths))
            db.add(ChallengeAttachmentDraft(id=submission_id, owner_id=self.owner))
            await db.commit()
        asset_ids = [await self.asset(submission_id, path=path, position=index) for index, path in enumerate(paths)]
        objects = {path: b"AP1 bytes" for path in paths}
        copied = []
        async def sdk(fn, *args):
            if fn is attachments.verify_challenge_object:
                self.assertIn(args[0], objects)
                return "verified"
            self.assertIs(fn, attachments.copy_pending_challenge_object)
            source, destination = args
            if source == paths[1]:
                raise OSError("owned SDK fake second-copy failure")
            objects[destination] = objects[source]
            copied.append(destination)
        async with self.session() as db:
            with patch.object(attachments, "run_in_threadpool", sdk), self.assertRaises(HTTPException) as caught:
                await approve_submission(str(submission_id), db=db, admin_id=self.owner, _role="admin")
            self.assertEqual(caught.exception.status_code, 503)
        async with self.session() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Challenge)), 0)
            self.assertEqual((await db.get(ChallengeSubmission, submission_id)).status, "pending")
            draft = await db.get(ChallengeAttachmentDraft, submission_id)
            self.assertEqual((draft.challenge_id, draft.state), (None, "open"))
            for identifier, source in zip(asset_ids, paths):
                row = await db.get(ChallengeAttachment, identifier)
                self.assertEqual((row.object_path, row.state, row.challenge_id, row.bound_challenge_id), (source, "pending", None, None))
        self.assertEqual(len(copied), 1)
        self.assertTrue(all(path in objects for path in paths))
        self.assertIn(copied[0], objects)

    async def test_same_parent_import_refs_and_foreign_or_missing_batch_rollback(self):
        parent_id = await self.parent(files=["legacy preserved.bin"])
        draft_id = await self.draft(parent_id)
        asset_id = await self.asset(draft_id, challenge_id=parent_id)
        async with self.session() as db:
            parent = await db.get(Challenge, parent_id)
            parent.attachment_revision = 1
            name = parent.name
            hint = Hint(challenge_id=parent_id, content="<p>Keep</p>", cost=0, order_index=0)
            flag = Flag(challenge_id=parent_id, type="static", value="ap1-owned-fixture", order_index=0)
            history = Submission(challenge_id=parent_id, user_id=self.owner, submitted_flag="ap1-owned", is_correct=True)
            db.add_all([hint, flag, history])
            await db.commit()
            old_ids = hint.id, flag.id, history.id
        async def import_rows(db, rows):
            return await import_challenge_batch(db, {"version": 2, "mode": "overwrite", "challenges": rows},
                actor_id=self.owner, validate_dynamic_policy=lambda _: None,
                invalidate_environment=lambda _: self.fail("unchanged environment invalidated"), revoke_active_issuances=AsyncMock())
        async with self.session() as db:
            result = await import_rows(db, [{"id": parent_id, "name": name, "attachment_ids": [str(asset_id)]}])
            self.assertEqual(result, {"created": 0, "updated": 1, "skipped": 0})
        for invalid in (asset_id, uuid4()):
            new_name = "ap1-import-forbidden-" + uuid4().hex
            async with self.session() as db:
                with self.assertRaises(HTTPException) as caught:
                    await import_rows(db, [{"id": 100001, "name": name, "description": "Must rollback", "attachment_ids": [str(asset_id)]},
                        {"id": 100002, "name": new_name, "attachment_ids": [str(invalid)]}])
                self.assertEqual(caught.exception.detail["code"], "attachment_reference_conflict")
            async with self.session() as db:
                parent = await db.get(Challenge, parent_id)
                self.assertEqual((parent.description, parent.files, parent.attachment_revision), ("<p>Before</p>", ["legacy preserved.bin"], 1))
                self.assertIsNone(await db.scalar(select(Challenge.id).where(Challenge.name == new_name)))
                self.assertEqual((await db.get(ChallengeAttachment, asset_id)).challenge_id, parent_id)
                for model, identifier in zip((Hint, Flag, Submission), old_ids):
                    self.assertIsNotNone(await db.get(model, identifier))


if __name__ == "__main__":
    unittest.main()
