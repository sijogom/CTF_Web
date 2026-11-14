"""필수 코스 그래프 편집의 트랜잭션 잠금 순서."""
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.course import Course


_GRAPH_LOCK_NAMESPACE = 0x43544657
_COURSE_PREREQUISITE_GRAPH_LOCK = 0x43505251


async def lock_course_prerequisite_mutation(
    db: AsyncSession, *, course_id: UUID, prerequisite_ids: list[UUID],
) -> Course | None:
    """전체 cycle 검사를 직렬화한 뒤 원본·대상을 ID 순서로 잠근다."""
    await db.execute(select(func.pg_advisory_xact_lock(
        _GRAPH_LOCK_NAMESPACE, _COURSE_PREREQUISITE_GRAPH_LOCK,
    )).execution_options(autoflush=False))
    rows = await db.execute(select(Course)
        .where(Course.id.in_({course_id, *prerequisite_ids})).order_by(Course.id)
        .with_for_update().execution_options(populate_existing=True, autoflush=False))
    return next((course for course in rows.scalars().all() if course.id == course_id), None)
