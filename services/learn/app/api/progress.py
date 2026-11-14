"""학습 진행도 API."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.course import Course, Lesson, UserProgress
from app.schemas.course import MyProgressResponse, ProgressResponse
from app.api.deps import get_current_user_id, get_optional_user_role
from app.utils.auth_client import verify_user_exists
from app.utils.course_prerequisites import get_course_prerequisite_status

router = APIRouter(tags=["진행도"])


@router.post("/progress/{lesson_id}/complete", response_model=ProgressResponse)
async def complete_lesson(
    lesson_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
    user_role: str | None = Depends(get_optional_user_role),
):
    """레슨 완료 처리."""
    # 사용자 존재 여부 검증 (크로스 DB)
    if not await verify_user_exists(str(user_id)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="사용자를 찾을 수 없습니다.",
        )

    # 보관 삭제된 코스의 레슨은 완료 처리할 수 없다.
    lesson_result = await db.execute(
        select(Lesson, Course)
        .join(Course, Course.id == Lesson.course_id)
        .where(Lesson.id == lesson_id, Course.is_deleted.is_(False))
    )
    lesson_row = lesson_result.first()
    if not lesson_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="레슨을 찾을 수 없습니다.")
    lesson, course = lesson_row
    if user_role not in ("admin", "moderator"):
        if not course.is_published or course.status != "approved":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="레슨을 찾을 수 없습니다.")
        locked, _details = await get_course_prerequisite_status(
            db,
            course_id=course.id,
            user_id=user_id,
        )
        if locked:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="선행 코스를 모두 완료한 뒤 이 레슨을 완료 처리할 수 있습니다.",
            )

    # 이미 완료했는지 확인
    existing = await db.execute(
        select(UserProgress).where(
            UserProgress.user_id == user_id,
            UserProgress.lesson_id == lesson_id,
        )
    )
    progress = existing.scalar_one_or_none()

    if progress:
        if progress.completed:
            return ProgressResponse(
                lesson_id=progress.lesson_id,
                completed=progress.completed,
                completed_at=progress.completed_at,
            )
        progress.completed = True
        progress.completed_at = datetime.now(timezone.utc)
    else:
        progress = UserProgress(
            user_id=user_id,
            lesson_id=lesson_id,
            completed=True,
            completed_at=datetime.now(timezone.utc),
        )
        db.add(progress)

    await db.commit()
    await db.refresh(progress)

    return ProgressResponse(
        lesson_id=progress.lesson_id,
        completed=progress.completed,
        completed_at=progress.completed_at,
    )


@router.get("/progress/me", response_model=list[MyProgressResponse])
async def my_progress(
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
):
    """내 전체 학습 진행도 조회.

    N+1 문제를 방지하기 위해 단일 집계 쿼리로 코스별 전체/완료 레슨 수를 계산한다.
    """
    # 1) 사용자가 진행 중인 코스 ID 집합
    progressing_subq = (
        select(Lesson.course_id)
        .join(UserProgress, UserProgress.lesson_id == Lesson.id)
        .where(UserProgress.user_id == user_id, UserProgress.completed.is_(True))
        .distinct()
        .subquery()
    )

    # 2) 해당 코스들의 전체 레슨 수 (단일 쿼리)
    total_subq = (
        select(
            Lesson.course_id.label("course_id"),
            func.count(Lesson.id).label("total_lessons"),
        )
        .where(Lesson.course_id.in_(select(progressing_subq.c.course_id)))
        .group_by(Lesson.course_id)
        .subquery()
    )

    # 3) 해당 코스들의 완료 레슨 수 (단일 쿼리)
    completed_subq = (
        select(
            Lesson.course_id.label("course_id"),
            func.count(UserProgress.id).label("completed_lessons"),
        )
        .join(UserProgress, UserProgress.lesson_id == Lesson.id)
        .where(
            Lesson.course_id.in_(select(progressing_subq.c.course_id)),
            UserProgress.user_id == user_id,
            UserProgress.completed.is_(True),
        )
        .group_by(Lesson.course_id)
        .subquery()
    )

    # 4) Course 정보 + 전체 레슨 수 + 완료 레슨 수를 JOIN으로 한번에 조회
    query = (
        select(
            Course.id,
            Course.title,
            total_subq.c.total_lessons,
            func.coalesce(completed_subq.c.completed_lessons, 0).label("completed_lessons"),
        )
        .join(total_subq, Course.id == total_subq.c.course_id)
        .outerjoin(completed_subq, Course.id == completed_subq.c.course_id)
        .where(Course.is_deleted.is_(False))
    )

    rows = await db.execute(query)

    return [
        MyProgressResponse(
            course_id=row.id,
            course_title=row.title,
            total_lessons=row.total_lessons,
            completed_lessons=row.completed_lessons,
            progress_percent=round(row.completed_lessons / row.total_lessons * 100, 1)
            if row.total_lessons > 0
            else 0.0,
        )
        for row in rows.all()
    ]
