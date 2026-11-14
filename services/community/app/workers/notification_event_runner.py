"""Community 댓글 outbox를 Auth로 전달한다. 메일을 직접 보내지 않는다."""

import asyncio
from contextlib import ExitStack, contextmanager
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import async_session, engine
from app.models.notification_event import CommentNotificationEvent
from app.models.post import Comment, Post, PostStatus
from app.workers.notification_health import NotificationWorkerHealth

logger = logging.getLogger(__name__)


def _restore_signal_handler(loop, signum, previous):
    try:
        loop.remove_signal_handler(signum)
    finally:
        signal.signal(signum, previous)


@contextmanager
def _cancel_on_shutdown_signals():
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("notification worker requires an asyncio task")
    requested = False

    def request_shutdown():
        nonlocal requested
        # Repeated signals must not interrupt the first cancellation's cleanup.
        if not requested:
            requested = True
            task.cancel()

    with ExitStack() as cleanup:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous = signal.getsignal(signum)
            try:
                loop.add_signal_handler(signum, request_shutdown)
            except NotImplementedError:
                signal.signal(signum, lambda *_args: loop.call_soon_threadsafe(request_shutdown))
                cleanup.callback(signal.signal, signum, previous)
            else:
                cleanup.callback(_restore_signal_handler, loop, signum, previous)
        yield


async def _close_worker_resources(health, client):
    # Preserve main's cancellation/failure while attempting every resource close.
    original = sys.exc_info()[1]
    failure = None
    cancellation = None
    resources = [("database", engine.dispose)]
    if client is not None:
        resources.insert(0, ("auth_client", client.aclose))
    if health is not None:
        resources.insert(0, ("health", health.close))
    for name, close in resources:
        try:
            await close()
        except BaseException as error:
            logger.error("comment_notification_runner.cleanup_failed resource=%s error=%s", name, type(error).__name__)
            if failure is None:
                failure = error
            if isinstance(error, asyncio.CancelledError):
                cancellation = error
    if cancellation is not None and not isinstance(original, asyncio.CancelledError):
        raise cancellation
    if original is None and failure is not None:
        raise failure


async def run_once(client, *, session_factory=async_session, limit=10, require_delivery_progress=False):
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        events = (await db.execute(select(CommentNotificationEvent).where(
            CommentNotificationEvent.completed_at.is_(None),
            CommentNotificationEvent.next_attempt_at <= now,
        ).order_by(CommentNotificationEvent.created_at).limit(limit)
            .with_for_update(skip_locked=True))).scalars().all()
        for event in events:
            post = await db.get(Post, event.post_id)
            comment = await db.get(Comment, event.comment_id)
            if (not post or not comment or post.is_hidden or post.status != PostStatus.APPROVED.value
                    or comment.is_hidden or comment.is_deleted or comment.author_id == event.recipient_id):
                event.completed_at = now
                continue
            event.attempt_count += 1
            try:
                response = await client.post(f"{settings.auth_service_url}/internal/personal-notifications",
                    json={"event_id": str(event.id), "recipient_id": str(event.recipient_id),
                          "post_id": str(event.post_id), "kind": "comment"})
                payload = response.json() if response.status_code == 200 else None
                accepted = isinstance(payload, dict) and payload.get("status") in {"accepted", "skipped"}
            except (httpx.HTTPError, ValueError):
                accepted = False
            if accepted:
                event.completed_at = now
                event.last_error = None
            else:
                event.last_error = "auth_delivery_unconfirmed"
                event.next_attempt_at = now + timedelta(seconds=min(3600, 2 ** min(event.attempt_count, 11)))
        failed_pending = None
        if require_delivery_progress:
            failed_pending = (await db.execute(select(CommentNotificationEvent.id).where(
                CommentNotificationEvent.completed_at.is_(None),
                CommentNotificationEvent.last_error.is_not(None),
            ).limit(1))).scalar_one_or_none()
        await db.commit()
        if failed_pending is not None:
            raise RuntimeError("comment delivery is awaiting retry")
        return len(events)


async def main():
    if not settings.internal_api_key:
        raise RuntimeError("INTERNAL_API_KEY is required")
    poll = float(os.environ.get("COMMENT_NOTIFICATION_POLL_SECONDS", "5"))
    if not 1 <= poll <= 300:
        raise RuntimeError("COMMENT_NOTIFICATION_POLL_SECONDS is outside the allowed range")
    with _cancel_on_shutdown_signals():
        health = None
        client = None
        try:
            health = NotificationWorkerHealth(int(os.environ.get("COMMENT_NOTIFICATION_HEALTH_PORT", "8330")), poll)
            await health.start()
            client = httpx.AsyncClient(timeout=8, headers={"X-Internal-Key": settings.internal_api_key})
            while True:
                try:
                    await run_once(client, require_delivery_progress=True)
                    health.mark_success()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("comment_notification_runner.tick_failed")
                await asyncio.sleep(poll)
        finally:
            await _close_worker_resources(health, client)


if __name__ == "__main__":
    asyncio.run(main())
