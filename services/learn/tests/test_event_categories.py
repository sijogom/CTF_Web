"""카테고리 입력과 PostgreSQL migration·동시 수정·연결 계약."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.events import (create_event, create_event_category, get_public_event, list_public_events,
    list_public_event_categories, update_event, update_event_category)
from app.models.event import PlatformEventCategory, PlatformEventCategoryAuditEvent
from app.schemas.event import EventCategoryCreate, EventCategoryUpdate, EventCreate, EventUpdate

DATABASE_URL = os.environ.get("EVENT_CATEGORY_TEST_DATABASE_URL") or os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
LABELS = {"ko": "새 행사", "en": "New event", "ja": "新しいイベント", "zh": "新活动"}


class EventCategorySchemaTests(unittest.TestCase):
    def test_all_languages_are_required_and_names_are_safe(self):
        self.assertEqual(EventCategoryCreate(labels=LABELS, sort_order=0).labels.ko, "새 행사")
        for labels in ({"ko": "이름"}, {**LABELS, "en": "   "}, {**LABELS, "ko": "<script>x</script>"}, {**LABELS, "zh": "x\x00"}, {**LABELS, "en": "x" * 81}):
            with self.subTest(labels=labels), self.assertRaises(ValidationError):
                EventCategoryCreate(labels=labels, sort_order=0)

    def test_revision_order_and_immutable_key_contract(self):
        for value in (-1, 1000001):
            with self.assertRaises(ValidationError):
                EventCategoryCreate(labels=LABELS, sort_order=value)
        with self.assertRaises(ValidationError):
            EventCategoryUpdate(labels=LABELS, sort_order=0, expected_revision=0)
        with self.assertRaises(ValidationError):
            EventCategoryUpdate(labels=LABELS, sort_order=0, expected_revision=1, key="changed")
        self.assertEqual(EventUpdate(category="c_123").category, "c_123")
        for key in ("", "한글", "bad key", "x" * 31):
            with self.assertRaises(ValidationError):
                EventUpdate(category=key)


@unittest.skipUnless(DATABASE_URL, "EVENT_CATEGORY_TEST_DATABASE_URL 작업 DB가 필요합니다.")
class EventCategoryPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "event_category_test_" + uuid4().hex
        self.admin_engine = create_async_engine(DATABASE_URL)
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_async_engine(DATABASE_URL, connect_args={"server_settings": {"search_path": self.schema}}, pool_size=5)
        self.actor = uuid4()
        self.legacy_id = uuid4()
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: self.migration(sync, "20260813_009_platform_events.py", "upgrade"))
            await connection.execute(text("INSERT INTO platform_events(id,title,content_markdown,category,starts_at,ends_at,visible_from_at,status,created_by,updated_by) VALUES(:id,'기존 행사','기존 본문','workshop',now(),now()+interval '1 hour',now()-interval '1 hour','published',:actor,:actor)"), {"id": self.legacy_id, "actor": self.actor})
            self.before = (await connection.execute(text("SELECT row_to_json(e) FROM platform_events e WHERE id=:id"), {"id": self.legacy_id})).scalar_one()
            await connection.run_sync(lambda sync: self.migration(sync, "20261013_029_event_category_registry.py", "upgrade"))

    def migration(self, connection, filename, action):
        spec = importlib.util.spec_from_file_location("event_category_test_migration", SERVICE_ROOT / "migrations" / "versions" / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(connection)):
            getattr(module, action)()

    async def asyncTearDown(self):
        await self.engine.dispose()
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        await self.admin_engine.dispose()

    def session(self):
        return AsyncSession(self.engine, expire_on_commit=False)

    async def new_category(self, labels=None, order=10):
        async with self.session() as db:
            return await create_event_category(EventCategoryCreate(labels=labels or LABELS, sort_order=order), db=db, actor_id=self.actor, _role="admin")

    async def test_migration_preserves_existing_event_and_seed_labels(self):
        async with self.session() as db:
            after = (await db.execute(text("SELECT row_to_json(e) FROM platform_events e WHERE id=:id"), {"id": self.legacy_id})).scalar_one()
            self.assertEqual(after, self.before)
            categories = await list_public_event_categories(db=db)
            self.assertEqual([item.key for item in categories.items], ["workshop", "seminar", "study", "networking", "notice", "other"])
            event = await get_public_event(self.legacy_id, db=db)
            self.assertEqual(event.category_labels, {"ko": "워크숍", "en": "Workshop", "ja": "ワークショップ", "zh": "工作坊"})

    async def test_add_rename_order_and_existing_event_follow_same_labels(self):
        category = await self.new_category()
        async with self.session() as db:
            now = datetime.now(timezone.utc)
            event = await create_event(EventCreate(title="새 분류 행사", content_markdown="행사 본문", category=category.key, starts_at=now, ends_at=now + timedelta(hours=1), visible_from_at=now - timedelta(hours=1), status="published"), db=db, actor_id=self.actor, _role="moderator")
            event_id = event.id
            self.assertEqual(event.category_labels, LABELS)
        changed = {**LABELS, "ko": "바뀐 행사", "en": "Changed event"}
        async with self.session() as db:
            result = await update_event_category(category.key, EventCategoryUpdate(labels=changed, sort_order=0, expected_revision=1), db=db, actor_id=self.actor, _role="moderator")
            self.assertEqual(result.revision, 2)
        async with self.session() as db:
            event = await get_public_event(event_id, db=db)
            self.assertEqual(event.category, category.key)
            self.assertEqual(event.category_labels, changed)
            listing = await list_public_events(category=category.key, include_ended=False, page=1, size=20, db=db)
            self.assertEqual([item.id for item in listing.items], [event_id])
            moved = await update_event(event_id, EventUpdate(category="seminar"), db=db, actor_id=self.actor, _role="admin")
            self.assertEqual(moved.category_labels["ko"], "세미나")
            self.assertEqual(await db.scalar(select(func.count()).select_from(PlatformEventCategoryAuditEvent)), 2)

    async def test_unknown_category_and_foreign_key_fail_closed(self):
        async with self.session() as db:
            with self.assertRaises(HTTPException) as caught:
                await list_public_events(category="unknown", include_ended=False, page=1, size=20, db=db)
            self.assertEqual(caught.exception.status_code, 422)
            await db.rollback()
            with self.assertRaises(HTTPException) as caught:
                await update_event(self.legacy_id, EventUpdate(category="unknown"), db=db, actor_id=self.actor, _role="admin")
            self.assertEqual(caught.exception.status_code, 422)
            await db.rollback()
            with self.assertRaises(IntegrityError):
                await db.execute(text("UPDATE platform_events SET category='unknown' WHERE id=:id"), {"id": self.legacy_id})
            await db.rollback()
            self.assertEqual((await get_public_event(self.legacy_id, db=db)).category, "workshop")

    async def test_concurrent_revision_only_one_update_and_audit_commits(self):
        category = await self.new_category()
        async def change(name):
            async with self.session() as db:
                try:
                    return await update_event_category(category.key, EventCategoryUpdate(labels={**LABELS, "ko": name}, sort_order=10, expected_revision=1), db=db, actor_id=self.actor, _role="admin")
                except HTTPException as error:
                    return error
        results = await asyncio.gather(change("동시 변경 A"), change("동시 변경 B"))
        self.assertEqual(sum(isinstance(item, HTTPException) and item.status_code == 409 for item in results), 1)
        async with self.session() as db:
            self.assertEqual((await db.get(PlatformEventCategory, category.key)).revision, 2)
            self.assertEqual(await db.scalar(select(func.count()).select_from(PlatformEventCategoryAuditEvent)), 2)

    async def test_registration_replay_and_concurrency_create_one_category_and_audit(self):
        operation_id = uuid4()
        body = EventCategoryCreate(labels=LABELS, sort_order=10, operation_id=operation_id)
        async def create():
            async with self.session() as db:
                return await create_event_category(body, db=db, actor_id=self.actor, _role="admin")
        first, second = await asyncio.gather(create(), create())
        self.assertEqual(first.key, second.key)
        self.assertEqual((await create()).key, first.key)
        async with self.session() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(PlatformEventCategory)), 7)
            self.assertEqual(await db.scalar(select(func.count()).select_from(PlatformEventCategoryAuditEvent)), 1)
            with self.assertRaises(HTTPException) as caught:
                await create_event_category(EventCategoryCreate(labels={**LABELS, "ko": "다른 요청"}, sort_order=10, operation_id=operation_id), db=db, actor_id=self.actor, _role="admin")
            self.assertEqual(caught.exception.detail["code"], "event_category_operation_conflict")
            await db.rollback()
            with self.assertRaises(HTTPException) as caught:
                await create_event_category(body, db=db, actor_id=uuid4(), _role="admin")
            self.assertEqual(caught.exception.status_code, 409)

    async def test_duplicate_name_rolls_back_category_revision_and_audit(self):
        category = await self.new_category()
        async with self.session() as db:
            with self.assertRaises(HTTPException) as caught:
                await update_event_category(category.key, EventCategoryUpdate(labels={**LABELS, "ko": "워크숍"}, sort_order=0, expected_revision=1), db=db, actor_id=self.actor, _role="admin")
            self.assertEqual(caught.exception.detail["code"], "event_category_duplicate")
            actual = await db.get(PlatformEventCategory, category.key)
            self.assertEqual(actual.revision, 1)
            self.assertEqual(actual.labels, LABELS)
            self.assertEqual(await db.scalar(select(func.count()).select_from(PlatformEventCategoryAuditEvent)), 1)
        with self.assertRaises(HTTPException) as caught:
            await self.new_category()
        self.assertEqual(caught.exception.status_code, 409)

    async def test_commit_failure_does_not_leave_category_without_audit(self):
        async with self.session() as db:
            with patch.object(db, "commit", side_effect=RuntimeError("injected commit failure")):
                with self.assertRaises(RuntimeError):
                    await create_event_category(EventCategoryCreate(labels=LABELS, sort_order=0), db=db, actor_id=self.actor, _role="admin")
        async with self.session() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(PlatformEventCategory)), 6)
            self.assertEqual(await db.scalar(select(func.count()).select_from(PlatformEventCategoryAuditEvent)), 0)

    async def test_downgrade_only_untouched_defaults_and_keeps_legacy_event(self):
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: self.migration(sync, "20261013_029_event_category_registry.py", "downgrade"))
            after = (await connection.execute(text("SELECT row_to_json(e) FROM platform_events e WHERE id=:id"), {"id": self.legacy_id})).scalar_one()
            self.assertEqual(after, self.before)
            await connection.run_sync(lambda sync: self.migration(sync, "20261013_029_event_category_registry.py", "upgrade"))
        await self.new_category()
        with self.assertRaisesRegex(RuntimeError, "destructive downgrade"):
            async with self.engine.begin() as connection:
                await connection.run_sync(lambda sync: self.migration(sync, "20261013_029_event_category_registry.py", "downgrade"))
        async with self.session() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(PlatformEventCategory)), 7)
            self.assertEqual(await db.scalar(select(func.count()).select_from(PlatformEventCategoryAuditEvent)), 1)


if __name__ == "__main__":
    unittest.main()
