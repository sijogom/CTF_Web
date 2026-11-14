"""문제 제출·실습 인스턴스가 공유하는 사용자별 접근 조건.

동적 플래그 발급은 컨테이너를 시작하기 전에 실제 정답을 전달하므로, 화면의 잠금 표시나
Env orchestrator의 선행 검사만 믿을 수 없다. 이 모듈은 제출 API와 같은 문제/학습
선행조건의 최소 판단을 Learn DB 기준으로 독립 수행한다.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge, ChallengePrerequisite, Submission
from app.models.course import ContentStatus, Course, Lesson, UserProgress
from app.models.knowledge import ChallengeLearningPrerequisite


async def user_can_access_challenge_for_instance(
    *,
    challenge_id: int,
    user_id: UUID,
    competition_id: str | None,
    db: AsyncSession,
) -> bool:
    """인스턴스 발급이 제출 선행조건을 우회하지 않는지 확인한다.

    - 워게임은 공개된 워게임 선행 문제의 일반 풀이만 인정한다.
    - CTF는 해당 대회 컨텍스트의 풀이만 인정한다. 대회 참가/배정/기간은 호출자가
      별도로 Auth service에서 검증해야 한다.
    - 과거에 삭제·비공개·미승인된 학습 선행 콘텐츠는 기존 제출 경로와 같이 영구
      잠금으로 해석하지 않는다.

    이 함수는 원문 플래그, 제출값, 컨테이너 식별자, 사용자 표시 이름을 반환하거나
    기록하지 않는다.
    """
    prerequisite_query = select(ChallengePrerequisite.prerequisite_id).where(
        ChallengePrerequisite.challenge_id == challenge_id
    )
    if competition_id is None:
        prerequisite_query = (
            prerequisite_query
            .join(Challenge, Challenge.id == ChallengePrerequisite.prerequisite_id)
            .where(
                Challenge.is_published.is_(True),
                Challenge.challenge_type == "wargame",
            )
        )
    prerequisite_ids = [row[0] for row in (await db.execute(prerequisite_query)).all()]
    if prerequisite_ids:
        solved_context = (
            Submission.competition_id == competition_id
            if competition_id is not None
            else Submission.competition_id.is_(None)
        )
        solved_ids = {
            row[0]
            for row in (
                await db.execute(
                    select(Submission.challenge_id).where(
                        Submission.user_id == user_id,
                        Submission.is_correct.is_(True),
                        Submission.challenge_id.in_(prerequisite_ids),
                        solved_context,
                    )
                )
            ).all()
        }
        if not all(prerequisite_id in solved_ids for prerequisite_id in prerequisite_ids):
            return False

    learning_prerequisites = (
        await db.execute(
            select(ChallengeLearningPrerequisite).where(
                ChallengeLearningPrerequisite.challenge_id == challenge_id
            )
        )
    ).scalars().all()
    for prerequisite in learning_prerequisites:
        if prerequisite.prerequisite_type == "course":
            course = await db.get(Course, prerequisite.prerequisite_target_id)
            if not (
                course
                and not course.is_deleted
                and course.is_published
                and course.status == ContentStatus.APPROVED.value
            ):
                continue
            total_lessons = await db.scalar(
                select(func.count(Lesson.id)).where(Lesson.course_id == course.id)
            )
            completed_lessons = await db.scalar(
                select(func.count(UserProgress.id))
                .join(Lesson, Lesson.id == UserProgress.lesson_id)
                .where(
                    Lesson.course_id == course.id,
                    UserProgress.user_id == user_id,
                    UserProgress.completed.is_(True),
                )
            )
            if not total_lessons or completed_lessons < total_lessons:
                return False
            continue

        if prerequisite.prerequisite_type == "lesson":
            lesson_row = (
                await db.execute(
                    select(Lesson, Course)
                    .join(Course, Course.id == Lesson.course_id)
                    .where(Lesson.id == prerequisite.prerequisite_target_id)
                )
            ).first()
            lesson = lesson_row[0] if lesson_row else None
            course = lesson_row[1] if lesson_row else None
            if not (
                lesson
                and course
                and not course.is_deleted
                and course.is_published
                and course.status == ContentStatus.APPROVED.value
            ):
                continue
            completed = await db.execute(
                select(UserProgress.id).where(
                    UserProgress.user_id == user_id,
                    UserProgress.lesson_id == lesson.id,
                    UserProgress.completed.is_(True),
                )
            )
            if completed.scalar_one_or_none() is None:
                return False
            continue

        # DB check constraint 밖의 손상된 type은 정책을 넓히지 않는다.
        return False

    return True
