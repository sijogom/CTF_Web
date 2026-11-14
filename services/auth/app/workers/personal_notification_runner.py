"""대회·배지 이벤트와 개인 이메일 outbox의 제한된 전달 worker."""

import asyncio
import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from html import escape
import logging
import os
import signal
import smtplib
import sys

from sqlalchemy import func, select, update

from app.config import settings
from app.database import async_session, engine
from app.email.sender import _get_smtp_config
from app.models.personal_notification import BadgeObservation, NotificationPolicy, PersonalNotification
from app.models.user import User
from app.utils.personal_notifications import create_due_competition_events, fanout_competition_events
from app.utils.internal_api import close_client
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


async def _close_worker_resources(health):
    # Preserve main's cancellation/failure while attempting every resource close.
    original = sys.exc_info()[1]
    failure = None
    cancellation = None
    resources = [("internal_client", close_client), ("database", engine.dispose)]
    if health is not None:
        resources.insert(0, ("health", health.close))
    for name, close in resources:
        try:
            await close()
        except BaseException as error:
            logger.error("personal_notification_runner.cleanup_failed resource=%s error=%s", name, type(error).__name__)
            if failure is None:
                failure = error
            if isinstance(error, asyncio.CancelledError):
                cancellation = error
    if cancellation is not None and not isinstance(original, asyncio.CancelledError):
        raise cancellation
    if original is None and failure is not None:
        raise failure


class EmailPreparationFailure(RuntimeError):
    """SMTP DATA 이전 실패만 안전하게 재시도한다."""


class KnownEmailOutage(RuntimeError):
    """미발송이 확인된 재시도 대기와 빈 큐를 구분한다."""


def _send_once(address, title, path, identity, config):
    from urllib.parse import urlsplit
    origin = settings.frontend_url.rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise EmailPreparationFailure("notification_frontend_url_invalid")
    # path는 생성 경로만 저장한다. 내용을 HTML로 재해석하지 않는다.
    link = f"{origin}/ko{path}"
    message = MIMEText(f'<p>{escape(title)}</p><p><a href="{escape(link, quote=True)}">알림 확인</a></p>', "html", "utf-8")
    message["Subject"] = title
    message["From"] = config.from_addr
    message["To"] = address
    message["Message-ID"] = f"<notification-{identity}@ctf-web.local>"
    factory = smtplib.SMTP_SSL if config.tls and config.port == 465 else smtplib.SMTP
    smtp = None
    try:
        smtp = factory(config.host, config.port, timeout=15)
        if config.tls and config.port != 465:
            smtp.starttls()
        if config.user:
            smtp.login(config.user, config.password)
    except Exception as failure:
        if smtp is not None:
            smtp.close()
        raise EmailPreparationFailure("smtp_preparation_failed") from failure
    try:
        smtp.sendmail(config.from_addr, address, message.as_string())
    finally:
        # DATA 수락 응답 뒤 QUIT 실패는 수락된 메일을 미발송으로 바꾸지 않는다.
        try:
            smtp.quit()
        except Exception:
            smtp.close()


async def deliver_one_email(*, session_factory=async_session, config_loader=_get_smtp_config, sender=_send_once):
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        await db.execute(update(PersonalNotification).where(
            PersonalNotification.email_status == "sending",
            PersonalNotification.email_started_at < now - timedelta(minutes=5),
        ).values(email_status="uncertain", last_error="delivery_interrupted"))
        item = (await db.execute(select(PersonalNotification).where(
            PersonalNotification.email_status == "pending", PersonalNotification.email_next_attempt_at <= now,
        ).order_by(PersonalNotification.created_at).limit(1).with_for_update(skip_locked=True))).scalar_one_or_none()
        if item is None:
            await db.commit()
            return False
        user = (await db.execute(select(User).where(User.id == item.user_id).with_for_update())).scalar_one_or_none()
        if (not user or not user.is_active or not user.email_verified or not user.email_notifications
                or not getattr(user, f"{item.kind}_notifications")):
            item.email_status = "skipped"
            await db.commit()
            return True
        try:
            config = await config_loader()
        except Exception:
            item.email_attempt_count += 1
            item.email_next_attempt_at = now + timedelta(seconds=min(3600, 2 ** min(item.email_attempt_count, 11)))
            item.last_error = "smtp_configuration_unavailable"
            await db.commit()
            raise KnownEmailOutage("smtp_configuration_unavailable")
        identity, recipient, title, path = item.id, user.email, item.title, item.path
        item.email_status = "sending"
        item.email_started_at = now
        item.email_attempt_count += 1
        await db.commit()
    # SMTP와 DB commit 사이의 불명확 결과를 자동 재전송하지 않는다.
    result, error = "sent", None
    try:
        await asyncio.to_thread(sender, recipient, title, path, identity, config)
    except asyncio.CancelledError:
        raise
    except EmailPreparationFailure:
        result, error = "pending", "smtp_preparation_failed"
    except Exception:
        result, error = "uncertain", "smtp_delivery_unconfirmed"
    async with session_factory() as db:
        values = {"email_status": result, "last_error": error}
        if result == "pending":
            values["email_next_attempt_at"] = datetime.now(timezone.utc) + timedelta(seconds=60)
        await db.execute(update(PersonalNotification).where(PersonalNotification.id == identity,
            PersonalNotification.email_status == "sending").values(**values))
        await db.commit()
    if result == "pending":
        raise KnownEmailOutage("smtp_preparation_failed")
    return True


async def run_email_batch(*, session_factory=async_session, deliver=deliver_one_email):
    for _ in range(10):
        if not await deliver(session_factory=session_factory):
            break
    # backoff 중에도 이미 확인된 장애를 빈 큐로 취급하지 않는다.
    async with session_factory() as db:
        failed = (await db.execute(select(PersonalNotification.id).where(
            PersonalNotification.email_status == "pending",
            PersonalNotification.last_error.in_(("smtp_configuration_unavailable", "smtp_preparation_failed")),
        ).limit(1))).scalar_one_or_none()
        if failed is not None:
            raise KnownEmailOutage("smtp_retry_pending")


async def run_event_batch(*, session_factory=async_session):
    async with session_factory() as db:
        await create_due_competition_events(db)
        await fanout_competition_events(db)
        await db.commit()


async def observe_badge_batch(cursor=None, *, session_factory=async_session, limit=10):
    from app.api.gamification import observe_user_badges
    async with session_factory() as db:
        query = select(User).where(User.is_active.is_(True))
        if cursor is not None:
            query = query.where(User.id > cursor)
        users = (await db.execute(query.order_by(User.id).limit(limit))).scalars().all()
        for user in users:
            _badges, complete = await observe_user_badges(user=user, db=db)
            if not complete:
                raise RuntimeError("badge observation dependencies unavailable")
        return users[-1].id if len(users) == limit else None


async def initialize_badge_baseline(*, session_factory=async_session):
    from app.api.gamification import BADGE_DEFINITIONS, observe_user_badges
    cursor, observed = None, 0
    while True:
        async with session_factory() as db:
            policy = await db.get(NotificationPolicy, "personal-v1")
            if policy is None:
                raise RuntimeError("notification migration is required")
            if policy.baseline_completed_at is not None:
                raise RuntimeError("badge baseline already completed")
            query = select(User).where(User.is_active.is_(True), User.created_at <= policy.activated_at)
            if cursor is not None:
                query = query.where(User.id > cursor)
            users = (await db.execute(query.order_by(User.id).limit(10))).scalars().all()
            for user in users:
                _badges, complete = await observe_user_badges(user=user, db=db)
                if not complete:
                    raise RuntimeError("badge baseline dependencies unavailable")
                count = (await db.execute(select(func.count()).select_from(BadgeObservation)
                    .where(BadgeObservation.user_id == user.id))).scalar_one()
                if count != len(BADGE_DEFINITIONS):
                    raise RuntimeError("badge baseline dependencies unavailable")
                observed += 1
                cursor = user.id
            if len(users) < 10:
                policy.baseline_completed_at = datetime.now(timezone.utc)
                await db.commit()
                return observed


async def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-only", action="store_true")
    args = parser.parse_args()
    with _cancel_on_shutdown_signals():
        health = None
        try:
            if args.baseline_only:
                count = await initialize_badge_baseline()
                logger.info("personal_notification_runner.baseline_complete count=%d", count)
                return
            async with async_session() as db:
                policy = await db.get(NotificationPolicy, "personal-v1")
                if policy is None or policy.baseline_completed_at is None:
                    raise RuntimeError("complete badge baseline before enabling notification worker")
            poll = float(os.environ.get("PERSONAL_NOTIFICATION_POLL_SECONDS", "10"))
            if not 1 <= poll <= 300:
                raise RuntimeError("PERSONAL_NOTIFICATION_POLL_SECONDS is outside the allowed range")
            health = NotificationWorkerHealth(int(os.environ.get("PERSONAL_NOTIFICATION_HEALTH_PORT", "8040")), poll)
            await health.start()
            cursor = None
            while True:
                try:
                    await run_event_batch()
                    cursor = await observe_badge_batch(cursor)
                    await run_email_batch()
                    health.mark_success()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    health.mark_failure()
                    logger.warning("personal_notification_runner.tick_failed")
                await asyncio.sleep(poll)
        finally:
            await _close_worker_resources(health)


if __name__ == "__main__":
    asyncio.run(main())
