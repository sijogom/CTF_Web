"""Pure helpers for guest inquiry proof, session tokens, and outbox retries."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID


GUEST_ACCESS_PROOF_TTL = timedelta(hours=24)
GUEST_SESSION_TTL = timedelta(minutes=30)
OUTBOX_LEASE_TTL = timedelta(minutes=5)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def token_digest(token: str) -> str:
    """Return the fixed-length SHA-256 digest used for opaque tokens and leases."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_opaque_token() -> str:
    """Generate a 256-bit URL-safe opaque token without persisting its raw form."""
    return secrets.token_urlsafe(32)


def _proof_message(challenge_id: UUID, expires_at: datetime) -> bytes:
    expires = int(expires_at.astimezone(timezone.utc).timestamp())
    return f"community-inquiry-access:v1:{challenge_id}:{expires}".encode("utf-8")


def sign_access_proof(challenge_id: UUID, expires_at: datetime, secret: str) -> str:
    """Create a deterministic, domain-separated HMAC proof for an email link."""
    if not secret:
        raise ValueError("inquiry access proof secret is unavailable")
    return hmac.new(
        secret.encode("utf-8"),
        _proof_message(challenge_id, expires_at),
        hashlib.sha256,
    ).hexdigest()


def verify_access_proof(
    challenge_id: UUID,
    expires_at: datetime,
    proof: str,
    secret: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Verify expiry and proof without revealing which predicate failed."""
    current = now or utc_now()
    if expires_at <= current or not secret:
        return False
    expected = sign_access_proof(challenge_id, expires_at, secret)
    return hmac.compare_digest(expected, proof)


def build_guest_access_path(
    challenge_id: UUID,
    inquiry_id: UUID,
    expires_at: datetime,
    secret: str,
) -> str:
    """Keep proof in the fragment so it is not sent as an HTTP URL query."""
    proof = sign_access_proof(challenge_id, expires_at, secret)
    return f"/inquiry/access#ticket={inquiry_id}&challenge={challenge_id}&proof={proof}"


def retry_delay(attempt_count: int) -> timedelta:
    """Bounded exponential retry delay for a runner-controlled outbox."""
    attempts = max(attempt_count, 1)
    return timedelta(seconds=min(60 * (2 ** (attempts - 1)), 3600))


def sanitize_delivery_error(error: str | None) -> str | None:
    """Keep runner errors bounded and prevent header/log injection through completion input."""
    if not error:
        return None
    return " ".join(error.replace("\r", " ").replace("\n", " ").split())[:500] or None
