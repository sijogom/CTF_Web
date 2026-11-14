"""댓글과 outbox의 실제 PostgreSQL 트랜잭션·재시도 인수."""

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.posts import CommentCreate, create_comment
from app.models.notification_event import CommentNotificationEvent
from app.models.post import Base, Comment, Post
from app.workers.notification_event_runner import run_once

TEST_URL = os.environ.get("PERSONAL_NOTIFICATION_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL, "작업 전용 PostgreSQL URL을 지정해야 합니다.")
class PostgreSQLCommentNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "comment_notification_test_" + uuid4().hex
        self.admin = create_async_engine(TEST_URL)
        async with self.admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_async_engine(TEST_URL, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[Post.__table__, Comment.__table__]))
        self.owner, self.writer, self.post_id = uuid4(), uuid4(), uuid4()
        async with self.sessions() as db:
            db.add(Post(id=self.post_id, title="post", content="<p>post</p>", board_type="free",
                        author_id=self.owner, author_name="owner", status="approved"))
            await db.flush()
            db.add(Comment(post_id=self.post_id, author_id=self.writer, author_name="old", content="old"))
            await db.commit()
        migration_path = Path(__file__).parents[1] / "migrations/versions/20261013_005_comment_notification_events.py"
        spec = importlib.util.spec_from_file_location("comment_notification_migration_acceptance", migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        async with self.engine.begin() as connection:
            def apply(sync):
                with patch.object(migration, "op", Operations(MigrationContext.configure(sync))):
                    migration.upgrade()
            await connection.run_sync(apply)

    async def asyncTearDown(self):
        await self.engine.dispose()
        async with self.admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        await self.admin.dispose()

    async def write_comment(self, author=None):
        async with self.sessions() as db:
            with patch("app.api.posts.verify_user_exists", new=AsyncMock(return_value=True)):
                return await create_comment(self.post_id, CommentCreate(content="new comment", is_anonymous=True),
                                            db, author or self.writer, "writer")

    async def test_existing_comments_are_not_backfilled_and_self_comment_is_silent(self):
        await self.write_comment(self.owner)
        async with self.sessions() as db:
            self.assertEqual((await db.execute(select(func.count()).select_from(Comment))).scalar_one(), 2)
            self.assertEqual((await db.execute(select(func.count()).select_from(CommentNotificationEvent))).scalar_one(), 0)

    async def test_concurrent_comments_each_have_event_and_counter_is_atomic(self):
        await asyncio.gather(self.write_comment(), self.write_comment())
        async with self.sessions() as db:
            self.assertEqual((await db.execute(select(func.count()).select_from(CommentNotificationEvent))).scalar_one(), 2)
            self.assertEqual((await db.get(Post, self.post_id)).comment_count, 2)
            self.assertEqual((await db.execute(select(func.count()).select_from(Comment))).scalar_one(), 3)

    async def test_outbox_failure_rolls_back_comment_and_counter(self):
        async with self.engine.begin() as connection:
            await connection.execute(text("ALTER TABLE comment_notification_events ADD CONSTRAINT test_reject_outbox CHECK (recipient_id IS NULL)"))
        with self.assertRaises(IntegrityError):
            await self.write_comment()
        async with self.sessions() as db:
            self.assertEqual((await db.execute(select(func.count()).select_from(Comment))).scalar_one(), 1)
            self.assertEqual((await db.get(Post, self.post_id)).comment_count, 0)

    async def test_delivery_response_loss_retries_same_event_and_completes_once(self):
        await self.write_comment()
        failed = SimpleNamespace(status_code=503)
        success = SimpleNamespace(status_code=200, json=lambda: {"status": "accepted", "created": False})
        client = SimpleNamespace(post=AsyncMock(side_effect=[failed, success]))
        await run_once(client, session_factory=self.sessions)
        async with self.sessions() as db:
            event = (await db.execute(select(CommentNotificationEvent))).scalar_one()
            self.assertIsNone(event.completed_at)
            event.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
        await run_once(client, session_factory=self.sessions)
        self.assertEqual(client.post.await_count, 2)
        first = client.post.await_args_list[0].kwargs["json"]
        second = client.post.await_args_list[1].kwargs["json"]
        self.assertEqual(first, second)
        self.assertNotIn("author", first)
        async with self.sessions() as db:
            self.assertIsNotNone((await db.execute(select(CommentNotificationEvent))).scalar_one().completed_at)
        self.assertEqual(await run_once(client, session_factory=self.sessions), 0)

    async def test_unconfirmed_pending_delivery_does_not_report_a_successful_tick(self):
        await self.write_comment()
        client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=503)))
        with self.assertRaisesRegex(RuntimeError, "awaiting retry"):
            await run_once(client, session_factory=self.sessions, require_delivery_progress=True)
        # backoff 도중 idle poll도 미확인 전달을 성공 tick으로 바꾸지 않는다.
        with self.assertRaisesRegex(RuntimeError, "awaiting retry"):
            await run_once(client, session_factory=self.sessions, require_delivery_progress=True)
        self.assertEqual(client.post.await_count, 1)
        async with self.sessions() as db:
            event = (await db.execute(select(CommentNotificationEvent))).scalar_one()
            self.assertIsNone(event.completed_at)
            self.assertEqual(event.last_error, "auth_delivery_unconfirmed")


if __name__ == "__main__":
    unittest.main()
