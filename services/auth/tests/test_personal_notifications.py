from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from app.api.me import NotificationPreferencesPatch, update_notification_preferences, update_email_preferences
from app.api.internal_notifications import CommentNotificationEvent, receive_comment_notification
from app.api.teams import get_team
from app.models.personal_notification import BadgeObservation, PersonalNotification
from app.utils.personal_notifications import enqueue_personal_notification, payload_digest, record_badge_observations
from app.workers.personal_notification_runner import EmailPreparationFailure, KnownEmailOutage, deliver_one_email, run_email_batch
from app.workers.notification_health import NotificationWorkerHealth


class Result:
    def __init__(self, value=None):
        self.value = value
    def scalar_one(self):
        return self.value
    def scalar_one_or_none(self):
        return self.value
    def scalars(self):
        return self
    def all(self):
        return self.value
    def __iter__(self):
        return iter(self.value)


class Session:
    def __init__(self, values=(), *, get_value=None):
        self.values = list(values)
        self.get_value = get_value
        self.added = []
        self.statements = []
        self.commits = 0
    async def execute(self, statement):
        self.statements.append(statement)
        if not self.values:
            raise AssertionError("unexpected query")
        return Result(self.values.pop(0))
    async def get(self, model, identity):
        return self.get_value
    async def commit(self):
        self.commits += 1
    def add(self, value):
        self.added.append(value)
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None


def user(**overrides):
    values = dict(id=uuid4(), username="member", is_active=True, email_verified=True,
                  email_notifications=False, comment_notifications=True,
                  competition_notifications=True, badge_notifications=True,
                  email="member@example.invalid", created_at=datetime.now(timezone.utc) - timedelta(days=5))
    return SimpleNamespace(**{**values, **overrides})


class PreferencesTests(unittest.IsolatedAsyncioTestCase):
    def test_strict_partial_preferences_reject_null_coercion_and_extra(self):
        for body in ({"comment": None}, {"comment": "false"}, {"email": 0}, {"unknown": True}):
            with self.subTest(body=body), self.assertRaises(ValidationError):
                NotificationPreferencesPatch(**body)
        self.assertEqual(NotificationPreferencesPatch(comment=False).model_dump(exclude_unset=True), {"comment": False})

    async def test_partial_update_preserves_email_and_other_categories(self):
        target, db = user(), Session()
        result = await update_notification_preferences(NotificationPreferencesPatch(comment=False), target, db)
        self.assertEqual(result, {"comment": False, "competition": True, "badge": True, "email": False})
        await update_email_preferences(enabled=True, user=target, db=db)
        self.assertTrue(target.email_notifications)
        self.assertFalse(target.comment_notifications)

    async def test_replayed_event_is_not_inserted_again_and_payload_conflict_rejected(self):
        target = user()
        digest = payload_digest("comment", "comment", "/community/example")
        db = Session([None, SimpleNamespace(payload_digest=digest)])
        created = await enqueue_personal_notification(db, user=target, event_id="event-1", kind="comment",
            title="comment", path="/community/example")
        self.assertFalse(created)
        db = Session([None, SimpleNamespace(payload_digest="different")])
        with self.assertRaises(HTTPException) as caught:
            await enqueue_personal_notification(db, user=target, event_id="event-1", kind="comment",
                title="comment", path="/community/example")
        self.assertEqual(caught.exception.status_code, 409)

    async def test_opted_out_event_has_permanent_invisible_receipt(self):
        db = Session([uuid4()])
        await enqueue_personal_notification(db, user=user(comment_notifications=False), event_id="event-2",
            kind="comment", title="comment", path="/community/example")
        values = db.statements[0].compile().params
        self.assertFalse(values["visible"])
        self.assertEqual(values["email_status"], "skipped")

    async def test_internal_missing_recipient_is_terminal_without_write(self):
        db = Session()
        result = await receive_comment_notification(CommentNotificationEvent(event_id=uuid4(), recipient_id=uuid4(), post_id=uuid4()), db)
        self.assertEqual(result, {"status": "skipped"})
        self.assertEqual(db.commits, 0)

    async def test_legacy_badges_baseline_is_silent_then_new_earned_is_once(self):
        now = datetime.now(timezone.utc)
        target = user()
        policy = SimpleNamespace(activated_at=now - timedelta(days=1), baseline_completed_at=now)
        badges = [SimpleNamespace(id="first_blood", name="first", earned=True),
                  SimpleNamespace(id="solver_10", name="ten", earned=False)]
        baseline = Session([None, []], get_value=policy)
        with patch("app.utils.personal_notifications.enqueue_personal_notification", new_callable=AsyncMock) as enqueue:
            await record_badge_observations(baseline, target, badges)
            enqueue.assert_not_awaited()
            self.assertEqual(len(baseline.added), 2)
            badges[1].earned = True
            followup = Session([None, baseline.added], get_value=policy)
            await record_badge_observations(followup, target, badges)
            enqueue.assert_awaited_once()
            again = Session([None, baseline.added], get_value=policy)
            await record_badge_observations(again, target, badges)
            enqueue.assert_awaited_once()

    async def test_new_member_first_real_badge_is_not_suppressed(self):
        now = datetime.now(timezone.utc)
        db = Session([None, []], get_value=SimpleNamespace(activated_at=now - timedelta(hours=1), baseline_completed_at=now))
        with patch("app.utils.personal_notifications.enqueue_personal_notification", new_callable=AsyncMock) as enqueue:
            await record_badge_observations(db, user(created_at=now), [SimpleNamespace(id="first_blood", name="first", earned=True)])
            enqueue.assert_awaited_once()

    async def test_team_detail_members_are_real_and_invite_is_membership_scoped(self):
        captain = user()
        team = SimpleNamespace(id=uuid4(), name="team", description="", invite_code="private-code",
                               captain_id=captain.id, total_score=13)
        members = [SimpleNamespace(id=captain.id, username=captain.username, xp=13)]
        outsider = await get_team(team.id, db=Session([team, members]), user=None)
        self.assertIsNone(outsider.invite_code)
        self.assertEqual(outsider.members[0].role, "captain")
        self.assertEqual(outsider.members[0].score, 13)
        insider = await get_team(team.id, db=Session([team, members, object()]), user=captain)
        self.assertEqual(insider.invite_code, "private-code")

    async def test_email_unknown_result_is_not_automatically_requeued(self):
        target = user(email_notifications=True)
        item = PersonalNotification(id=uuid4(), user_id=target.id, kind="comment", title="comment",
            path="/community/example", email_status="pending", email_attempt_count=0)
        claim = Session([None, item, target])
        finish = Session([None])
        sessions = iter([claim, finish])
        sender = lambda *args: (_ for _ in ()).throw(OSError("ambiguous acceptance"))
        await deliver_one_email(session_factory=lambda: next(sessions), config_loader=AsyncMock(return_value=object()), sender=sender)
        self.assertEqual(item.email_status, "sending")
        self.assertEqual(finish.statements[0].compile().params["email_status"], "uncertain")
        self.assertEqual(claim.commits, 1)

    async def test_email_opt_out_skips_before_loading_smtp(self):
        item = PersonalNotification(id=uuid4(), kind="badge", email_status="pending")
        db = Session([None, item, user(email_notifications=False)])
        loader = AsyncMock()
        await deliver_one_email(session_factory=lambda: db, config_loader=loader)
        loader.assert_not_awaited()
        self.assertEqual(item.email_status, "skipped")

    async def test_failure_before_smtp_data_can_retry_but_unknown_delivery_cannot(self):
        target = user(email_notifications=True)
        item = PersonalNotification(id=uuid4(), kind="comment", title="comment", path="/community/example",
                                    email_status="pending", email_attempt_count=0)
        claim, finish = Session([None, item, target]), Session([None])
        sessions = iter([claim, finish])
        def preparation_failure(*args):
            raise EmailPreparationFailure("connection unavailable before DATA")
        with self.assertRaises(KnownEmailOutage):
            await deliver_one_email(session_factory=lambda: next(sessions), config_loader=AsyncMock(return_value=object()), sender=preparation_failure)
        values = finish.statements[0].compile().params
        self.assertEqual(values["email_status"], "pending")
        self.assertGreater(values["email_next_attempt_at"], datetime.now(timezone.utc))

    async def test_smtp_config_failure_is_not_an_empty_queue(self):
        target = user(email_notifications=True)
        item = PersonalNotification(id=uuid4(), kind="comment", email_status="pending", email_attempt_count=0)
        db = Session([None, item, target])
        with self.assertRaises(KnownEmailOutage):
            await deliver_one_email(session_factory=lambda: db, config_loader=AsyncMock(side_effect=OSError("unavailable")))
        self.assertEqual(item.email_status, "pending")
        self.assertEqual(item.last_error, "smtp_configuration_unavailable")
        self.assertEqual(item.email_attempt_count, 1)
        self.assertGreater(item.email_next_attempt_at, datetime.now(timezone.utc))
        self.assertEqual(db.commits, 1)

    async def test_known_outage_remains_failed_during_retry_backoff(self):
        db = Session([uuid4()])
        deliver = AsyncMock(return_value=False)
        with self.assertRaises(KnownEmailOutage):
            await run_email_batch(session_factory=lambda: db, deliver=deliver)
        deliver.assert_awaited_once()
        query = db.statements[0].compile()
        self.assertIn("pending", query.params.values())
        self.assertIn(("smtp_configuration_unavailable", "smtp_preparation_failed"), [tuple(value) for value in query.params.values() if isinstance(value, (list, tuple))])

    async def test_empty_queue_without_known_retry_is_healthy(self):
        await run_email_batch(session_factory=lambda: Session([None]), deliver=AsyncMock(return_value=False))

    async def test_health_requires_successful_tick_and_baseline_and_expires(self):
        import asyncio
        import time
        health = NotificationWorkerHealth(8040, 1, baseline_ready=False)
        health.server = await asyncio.start_server(health._handle, "127.0.0.1", 0)
        port = health.server.sockets[0].getsockname()[1]
        async def request(path):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()
            return response
        try:
            self.assertIn(b"200 OK", await request("/health"))
            self.assertIn(b"503 Service Unavailable", await request("/ready"))
            health.mark_success()
            self.assertIn(b"503 Service Unavailable", await request("/ready"))
            health.baseline_ready = True
            self.assertIn(b"200 OK", await request("/ready"))
            health.mark_failure()
            self.assertIn(b"503 Service Unavailable", await request("/ready"))
            health.mark_success()
            self.assertIn(b"200 OK", await request("/ready"))
            health.last_success = time.monotonic() - health.max_age - 1
            self.assertIn(b"503 Service Unavailable", await request("/ready"))
        finally:
            await health.close()


if __name__ == "__main__":
    unittest.main()
