"""실제 작업용 PostgreSQL에서 고유 schema와 loopback SMTP만 사용한다."""

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path
import socketserver
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.me import NotificationPreferencesPatch, update_notification_preferences, update_email_preferences
from app.api.notification import notification_page
from app.api.teams import TeamCreate, TeamJoinRequest, create_team, get_team, join_team, regenerate_invite, remove_member
from app.email.sender import SmtpConfig
from app.models.competition import Competition
from app.models.notification import Notification
from app.models.team import Team, TeamMember
from app.models.personal_notification import BadgeObservation, CompetitionNotificationState, NotificationEvent, NotificationPolicy, PersonalNotification
from app.models.user import Base, User
from app.utils.personal_notifications import create_due_competition_events, enqueue_personal_notification, fanout_competition_events, record_badge_observations, record_competition_publish
from app.workers.personal_notification_runner import deliver_one_email

TEST_URL = os.environ.get("PERSONAL_NOTIFICATION_TEST_DATABASE_URL", "")


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 local notification acceptance\r\n")
        data = False
        lines = []
        while line := self.rfile.readline():
            if data:
                if line == b".\r\n":
                    self.server.messages.append(b"".join(lines))
                    if getattr(self.server, "disconnect_after_data", False):
                        return
                    lines = []
                    data = False
                    self.wfile.write(b"250 accepted\r\n")
                else:
                    lines.append(line)
            elif line.upper().startswith(b"DATA"):
                data = True
                self.wfile.write(b"354 data\r\n")
            elif line.upper().startswith(b"QUIT"):
                self.wfile.write(b"221 goodbye\r\n")
                break
            else:
                self.wfile.write(b"250 local\r\n")


@unittest.skipUnless(TEST_URL, "작업 전용 PostgreSQL URL을 지정해야 합니다.")
class PostgreSQLNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "notification_test_" + uuid4().hex
        self.admin = create_async_engine(TEST_URL)
        async with self.admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_async_engine(TEST_URL, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[User.__table__, Competition.__table__, Notification.__table__, Team.__table__, TeamMember.__table__]))
        self.user_id = uuid4()
        now = datetime.now(timezone.utc)
        self.existing_comp = uuid4()
        self.draft_comp = uuid4()
        async with self.sessions() as db:
            db.add(User(id=self.user_id, email="owner@example.invalid", username="owner", email_verified=True,
                        email_notifications=False, created_at=now - timedelta(days=1)))
            await db.flush()
            db.add_all([
                Competition(id=self.existing_comp, title="old", created_by=self.user_id, is_published=True,
                            start_at=now - timedelta(hours=1), end_at=now + timedelta(hours=1)),
                Competition(id=self.draft_comp, title="draft", created_by=self.user_id, is_published=False,
                            start_at=now + timedelta(hours=1), end_at=now + timedelta(hours=2)),
            ])
            await db.commit()
        migration_path = Path(__file__).parents[1] / "migrations/versions/20261013_005_personal_notifications.py"
        spec = importlib.util.spec_from_file_location("notification_migration_acceptance", migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        async with self.engine.begin() as connection:
            if self._testMethodName == "test_legacy_user_schema_adds_preferences_without_changing_email":
                for column in ("comment_notifications", "competition_notifications", "badge_notifications"):
                    await connection.execute(text(f"ALTER TABLE users DROP COLUMN {column}"))
            def apply(sync):
                with patch.object(migration, "op", Operations(MigrationContext.configure(sync))):
                    migration.upgrade()
            await connection.run_sync(apply)

    async def asyncTearDown(self):
        await self.engine.dispose()
        async with self.admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        await self.admin.dispose()

    async def test_concurrent_same_event_creates_one_receipt_and_email_task(self):
        async def create():
            async with self.sessions() as db:
                owner = await db.get(User, self.user_id)
                result = await enqueue_personal_notification(db, user=owner, event_id="comment:same", kind="comment",
                    title="comment", path="/community/example")
                await db.commit()
                return result
        self.assertEqual(sorted(await asyncio.gather(create(), create())), [False, True])
        async with self.sessions() as db:
            self.assertEqual((await db.execute(select(func.count()).select_from(PersonalNotification))).scalar_one(), 1)

    async def test_preference_refresh_and_legacy_email_share_storage(self):
        async with self.sessions() as db:
            owner = await db.get(User, self.user_id)
            await update_notification_preferences(NotificationPreferencesPatch(comment=False, badge=False), owner, db)
            await update_email_preferences(enabled=True, user=owner, db=db)
        async with self.sessions() as db:
            owner = await db.get(User, self.user_id)
            self.assertFalse(owner.comment_notifications)
            self.assertFalse(owner.badge_notifications)
            self.assertTrue(owner.competition_notifications)
            self.assertTrue(owner.email_notifications)

    async def test_legacy_user_schema_adds_preferences_without_changing_email(self):
        async with self.sessions() as db:
            owner = await db.get(User, self.user_id)
            self.assertTrue(owner.comment_notifications)
            self.assertTrue(owner.competition_notifications)
            self.assertTrue(owner.badge_notifications)
            self.assertFalse(owner.email_notifications)

    async def test_badge_baseline_then_concurrent_first_earning_is_one_event(self):
        baseline_badges = [SimpleNamespace(id="first_blood", name="first", earned=True),
                           SimpleNamespace(id="solver_10", name="ten", earned=False)]
        async with self.sessions() as db:
            policy = await db.get(NotificationPolicy, "personal-v1")
            policy.baseline_completed_at = datetime.now(timezone.utc)
            await record_badge_observations(db, await db.get(User, self.user_id), baseline_badges)
            await db.commit()
            self.assertEqual((await db.execute(select(func.count()).select_from(PersonalNotification))).scalar_one(), 0)
        baseline_badges[1].earned = True
        async def observe():
            async with self.sessions() as db:
                await record_badge_observations(db, await db.get(User, self.user_id), baseline_badges)
                await db.commit()
        await asyncio.gather(observe(), observe())
        async with self.sessions() as db:
            self.assertEqual((await db.execute(select(func.count()).select_from(PersonalNotification))).scalar_one(), 1)
            self.assertEqual((await db.execute(select(func.count()).select_from(BadgeObservation))).scalar_one(), 2)

    async def test_migration_suppresses_old_public_and_start_but_draft_first_publish_is_new(self):
        async with self.sessions() as db:
            old_state = await db.get(CompetitionNotificationState, self.existing_comp)
            self.assertTrue(old_state.published_recorded)
            self.assertTrue(old_state.started_recorded)
            self.assertFalse((await db.get(CompetitionNotificationState, self.draft_comp)).published_recorded)
            await create_due_competition_events(db)
            self.assertEqual((await db.execute(select(func.count()).select_from(NotificationEvent))).scalar_one(), 0)
            draft = await db.get(Competition, self.draft_comp)
            draft.is_published = True
            await record_competition_publish(db, draft)
            await record_competition_publish(db, draft)
            await db.commit()
        async with self.sessions() as db:
            self.assertEqual((await db.execute(select(func.count()).select_from(NotificationEvent))).scalar_one(), 1)
            await fanout_competition_events(db)
            await db.commit()
            self.assertEqual((await db.execute(select(func.count()).select_from(PersonalNotification))).scalar_one(), 1)

    async def test_notification_feed_is_owner_scoped_and_admin_global_only(self):
        async with self.sessions() as db:
            owner = await db.get(User, self.user_id)
            other = User(id=uuid4(), email="other@example.invalid", username="other")
            db.add(other)
            await db.flush()
            await enqueue_personal_notification(db, user=owner, event_id="comment:private", kind="comment",
                title="owner-only", path="/community/example")
            db.add(Notification(title="global", content="", type="toast"))
            await db.commit()
            self.assertEqual((await notification_page(db, owner.id, 1, 20))["total"], 2)
            self.assertEqual((await notification_page(db, other.id, 1, 20))["total"], 1)
            self.assertEqual((await notification_page(db, owner.id, 1, 20, global_only=True))["total"], 1)

    async def test_team_join_invite_rotation_and_removal_enforce_real_membership(self):
        from fastapi import HTTPException
        async with self.sessions() as db:
            owner = await db.get(User, self.user_id)
            member = User(id=uuid4(), email="member@example.invalid", username="member")
            outsider = User(id=uuid4(), email="outsider@example.invalid", username="outsider")
            db.add_all([member, outsider])
            await db.commit()
            team = await create_team(TeamCreate(name="acceptance-team"), db=db, user=owner)
            self.assertEqual(len(team.members), 1)
            self.assertIsNone((await get_team(team.id, db=db, user=member)).invite_code)
            await join_team(team.id, body=TeamJoinRequest(invite_code=team.invite_code), db=db, user=member)
            joined = await get_team(team.id, db=db, user=member)
            self.assertEqual(len(joined.members), 2)
            self.assertEqual(joined.invite_code, team.invite_code)
            with self.assertRaises(HTTPException) as rejected:
                await regenerate_invite(team.id, db=db, user=outsider)
            self.assertEqual(rejected.exception.status_code, 403)
            rotated = await regenerate_invite(team.id, db=db, user=owner)
            self.assertNotEqual(rotated["invite_code"], team.invite_code)
            self.assertEqual((await get_team(team.id, db=db, user=member)).invite_code, rotated["invite_code"])
            with self.assertRaises(HTTPException) as rejected:
                await remove_member(team.id, member.id, db=db, user=member)
            self.assertEqual(rejected.exception.status_code, 403)
            await remove_member(team.id, member.id, db=db, user=owner)
            removed = await get_team(team.id, db=db, user=member)
            self.assertIsNone(removed.invite_code)
            self.assertEqual(len(removed.members), 1)

    async def test_loopback_smtp_sends_once_for_replayed_event(self):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SMTPHandler)
        server.messages = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        async def config():
            return SmtpConfig(host="127.0.0.1", port=server.server_address[1], from_addr="sender@example.invalid",
                              tls=False, user="", password="")
        try:
            async with self.sessions() as db:
                owner = await db.get(User, self.user_id)
                owner.email_notifications = True
                await enqueue_personal_notification(db, user=owner, event_id="comment:smtp", kind="comment",
                    title="comment", path="/community/example")
                await db.commit()
            self.assertTrue(await deliver_one_email(session_factory=self.sessions, config_loader=config))
            self.assertFalse(await deliver_one_email(session_factory=self.sessions, config_loader=config))
            self.assertEqual(len(server.messages), 1)
            async with self.sessions() as db:
                item = (await db.execute(select(PersonalNotification))).scalar_one()
                self.assertEqual(item.email_status, "sent")
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=5)

    async def test_smtp_acceptance_response_loss_is_uncertain_and_never_resent(self):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SMTPHandler)
        server.messages = []
        server.disconnect_after_data = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        async def config():
            return SmtpConfig(host="127.0.0.1", port=server.server_address[1], from_addr="sender@example.invalid",
                              tls=False, user="", password="")
        try:
            async with self.sessions() as db:
                owner = await db.get(User, self.user_id)
                owner.email_notifications = True
                await enqueue_personal_notification(db, user=owner, event_id="comment:smtp-unknown", kind="comment",
                    title="comment", path="/community/example")
                await db.commit()
            await deliver_one_email(session_factory=self.sessions, config_loader=config)
            self.assertFalse(await deliver_one_email(session_factory=self.sessions, config_loader=config))
            self.assertEqual(len(server.messages), 1)
            async with self.sessions() as db:
                item = (await db.execute(select(PersonalNotification))).scalar_one()
                self.assertEqual(item.email_status, "uncertain")
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
