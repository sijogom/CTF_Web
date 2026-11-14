"""연속 출석(streak) 계산 헬퍼.

로그인/토큰 갱신 시 호출하여 streak_days를 업데이트한다.
DB 레벨 원자적 UPDATE로 동시 로그인 race condition을 방지한다.
"""

from datetime import datetime, timezone, timedelta

from sqlalchemy import case, cast, Date, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def update_streak(db: AsyncSession, user: User) -> None:
    """사용자의 streak_days와 last_active_at을 원자적으로 업데이트한다.

    - 같은 날 재접속: 변경 없음
    - 어제 접속한 경우: streak +1
    - 이틀 이상 미접속: streak = 1 (오늘부터 새로 시작)
    - 최초 접속 (last_active_at이 None): streak = 1
    """
    now = datetime.now(timezone.utc)
    today = now.date()

    # ORM 캐시에서 같은 날 재접속 체크 (불필요한 DB 호출 방지)
    if user.last_active_at is not None and user.last_active_at.date() == today:
        return

    yesterday = today - timedelta(days=1)

    # DB 레벨 원자적 업데이트 — 동시 요청이 와도 최종 결과는 동일
    new_streak = case(
        # 어제 접속 → streak + 1
        (cast(User.last_active_at, Date) == yesterday, User.streak_days + 1),
        # 그 외 (2일+ 미접속 또는 최초) → 1
        else_=1,
    )

    await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(streak_days=new_streak, last_active_at=now)
    )

    # ORM 캐시 동기화
    await db.refresh(user, ["streak_days", "last_active_at"])
