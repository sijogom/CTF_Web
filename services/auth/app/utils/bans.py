"""밴 상태 계산과 공개 응답에 사용하는 공통 함수."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jwt.token import create_ban_appeal_token
from app.models.ban import AccountBan
from app.utils.ban_state import is_ban_active, utc_now


async def get_active_ban(
    db: AsyncSession,
    user_id: UUID | str,
    *,
    now: datetime | None = None,
    for_update: bool = False,
) -> AccountBan | None:
    current = now or utc_now()
    query = (
        select(AccountBan)
        .where(
            AccountBan.user_id == user_id,
            AccountBan.started_at <= current,
            AccountBan.lifted_at.is_(None),
            or_(AccountBan.expires_at.is_(None), AccountBan.expires_at > current),
        )
        .order_by(AccountBan.started_at.desc())
        .limit(1)
    )
    if for_update:
        query = query.with_for_update()
    result = await db.execute(query)
    return result.scalar_one_or_none()


def public_ban_details(ban: AccountBan) -> dict[str, Any]:
    return {
        "id": str(ban.id),
        "reason": ban.public_reason,
        "started_at": ban.started_at.isoformat(),
        "expires_at": ban.expires_at.isoformat() if ban.expires_at else None,
        "appeal_allowed": bool(ban.appeal_allowed),
    }


def banned_error_detail(ban: AccountBan, user_id: UUID | str) -> dict[str, Any]:
    """인증된 대상자에게만 전달하는 최소 밴·이의제기 정보."""
    detail = {
        "code": "ACCOUNT_BANNED",
        "ban": public_ban_details(ban),
    }
    if ban.appeal_allowed:
        detail["appeal_token"] = create_ban_appeal_token(str(user_id), str(ban.id))
    return detail
