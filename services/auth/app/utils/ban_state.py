"""외부 설정 없이 검증 가능한 밴 시간 창 계산."""

from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def is_ban_active(ban: Any, *, now: datetime | None = None) -> bool:
    current = now or utc_now()
    if ban.lifted_at is not None or ban.started_at > current:
        return False
    return ban.expires_at is None or ban.expires_at > current
