from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.api.posts import CommentCreate, create_comment
from app.models.notification_event import CommentNotificationEvent
from app.workers.notification_event_runner import run_once


class Result:
    def __init__(self, values):
        self.values = values
    def scalars(self):
        return self
    def all(self):
        return self.values


class Session:
    def __init__(self, events=(), objects=()):
        self.events = list(events)
        self.objects = iter(objects)
        self.added = []
        self.commits = 0
    def add(self, item):
        self.added.append(item)
    async def flush(self):
        for item in self.added:
            if item.id is None:
                item.id = uuid4()
    async def execute(self, statement):
        return Result(self.events)
    async def commit(self):
        self.commits += 1
    async def refresh(self, item):
        item.created_at = datetime.now(timezone.utc)
    async def get(self, model, identity):
        return next(self.objects)
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None


class CommentNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_author_comment_and_event_commit_together_without_identity_payload(self):
        writer, owner, post_id = uuid4(), uuid4(), uuid4()
        post = SimpleNamespace(id=post_id, author_id=owner, status="approved", is_hidden=False)
        db = Session()
        with patch("app.api.posts.verify_user_exists", new=AsyncMock(return_value=True)), patch(
            "app.api.posts._get_post_for_visibility", new=AsyncMock(return_value=post)):
            await create_comment(post_id, CommentCreate(content="comment", is_anonymous=True), db, writer, "private-writer")
        events = [item for item in db.added if isinstance(item, CommentNotificationEvent)]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].recipient_id, owner)
        self.assertEqual(db.commits, 1)
        self.assertFalse(hasattr(events[0], "author_name"))

    async def test_self_comment_has_no_notification_event(self):
        owner, post_id = uuid4(), uuid4()
        post = SimpleNamespace(id=post_id, author_id=owner, status="approved", is_hidden=False)
        db = Session()
        with patch("app.api.posts.verify_user_exists", new=AsyncMock(return_value=True)), patch(
            "app.api.posts._get_post_for_visibility", new=AsyncMock(return_value=post)):
            await create_comment(post_id, CommentCreate(content="comment"), db, owner, "owner")
        self.assertFalse(any(isinstance(item, CommentNotificationEvent) for item in db.added))

    async def test_retry_preserves_event_id_after_response_loss(self):
        event = CommentNotificationEvent(id=uuid4(), recipient_id=uuid4(), post_id=uuid4(), comment_id=uuid4(), attempt_count=0)
        post = SimpleNamespace(is_hidden=False, status="approved")
        comment = SimpleNamespace(is_hidden=False, is_deleted=False, author_id=uuid4())
        db = Session([event], [post, comment])
        client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=503)))
        await run_once(client, session_factory=lambda: db)
        self.assertIsNone(event.completed_at)
        self.assertEqual(event.last_error, "auth_delivery_unconfirmed")
        self.assertEqual(client.post.call_args.kwargs["json"]["event_id"], str(event.id))

    async def test_hidden_comment_is_terminal_without_auth_delivery(self):
        event = CommentNotificationEvent(id=uuid4(), recipient_id=uuid4(), post_id=uuid4(), comment_id=uuid4(), attempt_count=0)
        db = Session([event], [SimpleNamespace(is_hidden=False, status="approved"), SimpleNamespace(is_hidden=True)])
        client = SimpleNamespace(post=AsyncMock())
        await run_once(client, session_factory=lambda: db)
        client.post.assert_not_awaited()
        self.assertIsNotNone(event.completed_at)


if __name__ == "__main__":
    unittest.main()
