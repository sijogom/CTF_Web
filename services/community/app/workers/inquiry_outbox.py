"""Claim/complete primitives for Community inquiry notification delivery.

This module deliberately does not run an infinite loop or call SMTP. A trusted
runner claims a command, calls Auth's internal notification API, then completes
the lease with the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.inquiry import (
    InquiryAccessChallenge,
    InquiryEvent,
    InquiryNotificationOutbox,
)
from app.utils.inquiry_ticket import (
    GUEST_ACCESS_PROOF_TTL,
    OUTBOX_LEASE_TTL,
    build_guest_access_path,
    issue_opaque_token,
    retry_delay,
    sanitize_delivery_error,
    token_digest,
    utc_now,
)


TERMINAL_OUTBOX_STATES = frozenset({"sent", "skipped"})
COMPLETE_STATES = frozenset({"sent", "skipped", "retry"})


@dataclass(frozen=True)
class ClaimedInquiryNotification:
    outbox_id: UUID
    lease_token: str
    recipient_type: str
    recipient_email: str | None
    recipient_user_id: UUID | None
    kind: str
    access_path: str
    locale: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "event_id": str(self.outbox_id),
            "lease_token": self.lease_token,
            "recipient_type": self.recipient_type,
            "recipient_email": self.recipient_email,
            "recipient_user_id": str(self.recipient_user_id) if self.recipient_user_id else None,
            "kind": self.kind,
            "access_path": self.access_path,
            "locale": self.locale,
        }


def _challenge_purpose(outbox: InquiryNotificationOutbox) -> str:
    return "reply_access" if outbox.kind == "reply_available" else "guest_access"


async def claim_inquiry_notifications(
    db: AsyncSession,
    *,
    worker_id: str,
    limit: int,
    proof_secret: str,
    now: datetime | None = None,
) -> list[ClaimedInquiryNotification]:
    """Atomically lease deliverable rows and return only the information Auth needs."""
    current = now or utc_now()
    candidates = await db.execute(
        select(InquiryNotificationOutbox)
        .options(
            selectinload(InquiryNotificationOutbox.inquiry),
            selectinload(InquiryNotificationOutbox.access_challenge),
        )
        .where(
            InquiryNotificationOutbox.available_at <= current,
            or_(
                InquiryNotificationOutbox.state.in_(("pending", "retry")),
                and_(
                    InquiryNotificationOutbox.state == "processing",
                    InquiryNotificationOutbox.lease_expires_at.is_not(None),
                    InquiryNotificationOutbox.lease_expires_at <= current,
                ),
            ),
        )
        .order_by(InquiryNotificationOutbox.available_at, InquiryNotificationOutbox.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    outbox_rows = candidates.scalars().all()

    for outbox in outbox_rows:
        lease_token = issue_opaque_token()
        outbox.state = "processing"
        outbox.attempt_count += 1
        outbox.lease_owner = worker_id
        outbox.lease_token_digest = token_digest(lease_token)
        outbox.lease_expires_at = current + OUTBOX_LEASE_TTL
        outbox.last_error = None
        # Stash the raw lease only for this process; the DB retains its digest.
        setattr(outbox, "_claim_lease_token", lease_token)

        if outbox.recipient_type == "guest":
            challenge = outbox.access_challenge
            if challenge is None or challenge.expires_at <= current:
                challenge = InquiryAccessChallenge(
                    inquiry_id=outbox.inquiry_id,
                    purpose=_challenge_purpose(outbox),
                    expires_at=current + GUEST_ACCESS_PROOF_TTL,
                )
                db.add(challenge)
                outbox.access_challenge = challenge
                db.add(
                    InquiryEvent(
                        inquiry_id=outbox.inquiry_id,
                        actor_type="system",
                        actor_id=None,
                        action="inquiry.guest_access_challenge_issued",
                        details={"outbox_id": str(outbox.id), "kind": outbox.kind},
                    )
                )

    await db.flush()

    claimed: list[ClaimedInquiryNotification] = []
    for outbox in outbox_rows:
        lease_token = getattr(outbox, "_claim_lease_token")
        delattr(outbox, "_claim_lease_token")
        if outbox.recipient_type == "guest":
            challenge = outbox.access_challenge
            assert challenge is not None
            access_path = build_guest_access_path(
                challenge.id,
                outbox.inquiry_id,
                challenge.expires_at,
                proof_secret,
            )
            recipient_email = outbox.inquiry.email
            recipient_user_id = None
        else:
            access_path = "/inquiry"
            recipient_email = None
            recipient_user_id = outbox.recipient_user_id

        claimed.append(
            ClaimedInquiryNotification(
                outbox_id=outbox.id,
                lease_token=lease_token,
                recipient_type=outbox.recipient_type,
                recipient_email=recipient_email,
                recipient_user_id=recipient_user_id,
                kind=outbox.kind,
                access_path=access_path,
                locale=outbox.locale if outbox.locale in {"ko", "en", "ja", "zh"} else "ko",
            )
        )
    return claimed


async def complete_inquiry_notification(
    db: AsyncSession,
    *,
    outbox_id: UUID,
    lease_token: str,
    result: str,
    error: str | None = None,
    now: datetime | None = None,
) -> str:
    """Finalize or schedule a retry only for the current lease holder."""
    if result not in COMPLETE_STATES:
        raise ValueError("unsupported inquiry notification completion state")

    current = now or utc_now()
    record = await db.execute(
        select(InquiryNotificationOutbox)
        .where(InquiryNotificationOutbox.id == outbox_id)
        .with_for_update()
    )
    outbox = record.scalar_one_or_none()
    if outbox is None:
        raise LookupError("inquiry notification outbox item does not exist")
    if outbox.state in TERMINAL_OUTBOX_STATES:
        return outbox.state
    if outbox.state != "processing" or not outbox.lease_token_digest:
        raise PermissionError("inquiry notification is not currently leased")
    if outbox.lease_expires_at is None or outbox.lease_expires_at <= current:
        raise PermissionError("inquiry notification lease expired")
    if not hmac_compare_digest(outbox.lease_token_digest, token_digest(lease_token)):
        raise PermissionError("inquiry notification lease does not match")

    outbox.lease_token_digest = None
    outbox.lease_owner = None
    outbox.lease_expires_at = None
    outbox.last_error = sanitize_delivery_error(error)

    if result == "retry":
        outbox.state = "retry"
        outbox.available_at = current + retry_delay(outbox.attempt_count)
        event_action = "inquiry.notification_retry"
    else:
        outbox.state = result
        outbox.sent_at = current
        event_action = "inquiry.notification_sent" if result == "sent" else "inquiry.notification_skipped"

    db.add(
        InquiryEvent(
            inquiry_id=outbox.inquiry_id,
            actor_type="system",
            actor_id=None,
            action=event_action,
            details={"outbox_id": str(outbox.id), "attempt": outbox.attempt_count},
        )
    )
    await db.flush()
    return outbox.state


def hmac_compare_digest(left: str, right: str) -> bool:
    """Small testable wrapper that keeps completion comparison constant-time."""
    import hmac

    return hmac.compare_digest(left, right)
