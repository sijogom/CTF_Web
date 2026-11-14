"""코스 강제 선행조건의 공통 검증 로직.

코스 목록, 상세, 레슨 직접 URL, 완료 처리에서 같은 판정을 써야 우회 경로가 생기지
않는다. 삭제·비공개·미승인 선행 코스는 운영 정리 전의 오래된 참조일 수 있으므로
사용자를 영구 잠금하지 않고 로그로만 남긴다.
"""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.course import Course, CoursePrerequisite, Lesson, UserProgress
from app.utils.course_prerequisite_rules import is_public_course


logger = logging.getLogger(__name__)


async def get_course_prerequisite_status(
    db: AsyncSession,
    *,
    course_id: UUID,
    user_id: UUID | None,
) -> tuple[bool, list[dict]]:
    """현재 사용자의 선행 코스 충족 여부와 안전한 설명 목록을 반환한다.

    `CoursePrerequisite`는 현재 코스가 요구하는 선행 코스를 가리킨다. 공개 가능하며
    레슨이 하나 이상인 선행 코스만 강제 조건으로 계산한다. 나머지는 데이터 정리 전
    임시 상태로 보고 사용자 흐름을 막지 않는다.
    """
    result = await db.execute(
        select(CoursePrerequisite, Course)
        .join(Course, Course.id == CoursePrerequisite.prerequisite_id)
        .where(CoursePrerequisite.course_id == course_id)
        .order_by(Course.title)
    )
    rows = result.all()
    if not rows:
        return False, []

    details: list[dict] = []
    all_completed = True
    for prerequisite, course in rows:
        if not is_public_course(course):
            logger.warning(
                "사용할 수 없는 코스 선행조건을 건너뜁니다: course=%s prerequisite=%s",
                course_id,
                prerequisite.prerequisite_id,
            )
            continue

        total_lessons = int(
            (await db.scalar(select(func.count(Lesson.id)).where(Lesson.course_id == course.id))) or 0
        )
        if total_lessons == 0:
            logger.warning(
                "레슨이 없는 코스 선행조건을 건너뜁니다: course=%s prerequisite=%s",
                course_id,
                course.id,
            )
            continue

        completed_lessons = 0
        if user_id:
            completed_lessons = int(
                (await db.scalar(
                    select(func.count(UserProgress.id))
                    .join(Lesson, Lesson.id == UserProgress.lesson_id)
                    .where(
                        Lesson.course_id == course.id,
                        UserProgress.user_id == user_id,
                        UserProgress.completed.is_(True),
                    )
                )) or 0
            )
        completed = completed_lessons >= total_lessons
        details.append(
            {
                "id": str(course.id),
                "title": course.title,
                "completed": completed,
                "completed_lessons": completed_lessons,
                "total_lessons": total_lessons,
            }
        )
        if not completed:
            all_completed = False

    return bool(details) and not all_completed, details
