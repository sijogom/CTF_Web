"""소유 트랜잭션 안에서 최초 이벤트와 사용자별 멱등 receipt를 저장한다."""

import hashlib
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.competition import Competition
from app.models.personal_notification import (
    BadgeObservation, CompetitionNotificationState, NotificationEvent,
    NotificationPolicy, PersonalNotification,
)
from app.models.user import User


KINDS = frozenset({"comment", "competition", "badge"})


def payload_digest(kind: str, title: str, path: str) -> str:
    return hashlib.sha256(json.dumps([kind, title, path], ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


async def enqueue_personal_notification(db, *, user: User, event_id: str,
                                        kind: str, title: str, path: str):
    digest = payload_digest(kind, title, path)
    enabled = user.is_active and bool(getattr(user, f"{kind}_notifications"))
    result = await db.execute(insert(PersonalNotification).values(
        user_id=user.id, event_id=event_id, payload_digest=digest, kind=kind,
        title=title, path=path, visible=enabled,
        email_status="pending" if enabled and user.email_notifications and user.email_verified else "skipped",
    ).on_conflict_do_nothing(index_elements=["user_id", "event_id"]).returning(PersonalNotification.id))
    new_id = result.scalar_one_or_none()
    if new_id is None:
        existing = (await db.execute(select(PersonalNotification).where(
            PersonalNotification.user_id == user.id, PersonalNotification.event_id == event_id,
        ))).scalar_one()
        if existing.payload_digest != digest:
            raise HTTPException(status_code=409, detail="이미 처리한 알림 이벤트의 내용이 다릅니다.")
    return new_id is not None


async def record_competition_publish(db, competition: Competition):
    await db.execute(insert(CompetitionNotificationState).values(
        competition_id=competition.id, published_recorded=False, started_recorded=False,
    ).on_conflict_do_nothing())
    state = (await db.execute(select(CompetitionNotificationState).where(
        CompetitionNotificationState.competition_id == competition.id,
    ).with_for_update())).scalar_one()
    if competition.is_published and not state.published_recorded:
        state.published_recorded = True
        await db.execute(insert(NotificationEvent).values(
            event_id=f"competition:{competition.id}:published", competition_id=competition.id,
            title="새 대회가 공개되었습니다.", path=f"/ctf/{competition.id}",
        ).on_conflict_do_nothing())


async def record_badge_observations(db, user: User, badges):
    # 같은 사용자의 동시 조회와 worker가 하나의 기준선/최초 획득만 기록한다.
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    policy = await db.get(NotificationPolicy, "personal-v1")
    if policy is None:
        raise RuntimeError("notification migration is required")
    existing = {row.badge_id: row for row in (await db.execute(select(BadgeObservation).where(
        BadgeObservation.user_id == user.id,
    ))).scalars()}
    first_observation = not existing
    legacy = user.created_at < policy.activated_at
    now = datetime.now(timezone.utc)
    for badge in badges:
        observed = existing.get(badge.id)
        prior_earned = observed.earned if observed is not None else False
        if observed is None:
            observed = BadgeObservation(user_id=user.id, badge_id=badge.id, earned=False)
            db.add(observed)
        if badge.earned and not prior_earned:
            observed.earned = True
            observed.earned_at = now
            if policy.baseline_completed_at is not None and not (first_observation and legacy):
                await enqueue_personal_notification(db, user=user,
                    event_id=f"badge:{user.id}:{badge.id}", kind="badge",
                    title=f"새 배지: {badge.name}"[:200], path="/profile")
        observed.observed_at = now


async def create_due_competition_events(db, limit=20):
    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(CompetitionNotificationState, Competition).join(
        Competition, Competition.id == CompetitionNotificationState.competition_id,
    ).where(CompetitionNotificationState.started_recorded.is_(False),
            Competition.is_published.is_(True), Competition.is_deleted.is_(False),
            Competition.start_at <= now).order_by(Competition.start_at).limit(limit)
        .with_for_update(skip_locked=True))).all()
    for state, competition in rows:
        state.started_recorded = True
        if competition.end_at > now:
            await db.execute(insert(NotificationEvent).values(
                event_id=f"competition:{competition.id}:started", competition_id=competition.id,
                title="대회가 시작되었습니다.", path=f"/ctf/{competition.id}",
            ).on_conflict_do_nothing())
    return len(rows)


async def fanout_competition_events(db, batch_size=100):
    event = (await db.execute(select(NotificationEvent).where(NotificationEvent.complete.is_(False))
        .order_by(NotificationEvent.created_at, NotificationEvent.event_id).limit(1)
        .with_for_update(skip_locked=True))).scalar_one_or_none()
    if event is None:
        return 0
    competition = await db.get(Competition, event.competition_id)
    if not competition or not competition.is_published or competition.is_deleted:
        event.complete = True
        return 0
    query = select(User).where(User.is_active.is_(True), User.created_at <= event.created_at)
    if event.last_user_id is not None:
        query = query.where(User.id > event.last_user_id)
    users = (await db.execute(query.order_by(User.id).limit(batch_size))).scalars().all()
    for user in users:
        await enqueue_personal_notification(db, user=user, event_id=event.event_id,
            kind=event.kind, title=event.title, path=event.path)
        event.last_user_id = user.id
    event.complete = len(users) < batch_size
    return len(users)
