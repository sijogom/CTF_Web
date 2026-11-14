"""교육 도메인의 UTC 기간과 수강생 공개 판정 순수 함수."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def is_active_enrollment(status: str | None) -> bool:
    return status == "active"


def is_student_class_visible(*, class_status: str, enrollment_status: str | None) -> bool:
    """수강생은 활성 수강 상태이고 공개 또는 보관된 강의만 볼 수 있다."""
    return is_active_enrollment(enrollment_status) and class_status in {"published", "archived"}


def is_student_assignment_visible(
    *,
    class_status: str,
    enrollment_status: str | None,
    assignment_status: str,
) -> bool:
    """draft 과제는 교수/교육 관리자에게만 보이고, 보관 과제는 이력 열람만 허용한다."""
    return (
        is_student_class_visible(
            class_status=class_status,
            enrollment_status=enrollment_status,
        )
        and assignment_status in {"published", "archived"}
    )


def is_submission_window_open(
    *,
    class_status: str,
    enrollment_status: str | None,
    assignment_status: str,
    available_from_at: datetime | None,
    due_at: datetime | None,
    now: datetime,
) -> bool:
    """UTC 기준 제출 가능 기간. 보관 상태에서는 신규·수정 제출을 차단한다."""
    # 보관된 강의는 과거 자료·성적 이력만 열람할 수 있고 신규/수정 제출은 막는다.
    if class_status != "published" or not is_active_enrollment(enrollment_status):
        return False
    if assignment_status != "published":
        return False
    if available_from_at is not None and now < available_from_at:
        return False
    if due_at is not None and now > due_at:
        return False
    return True


def can_student_view_grade(*, grade_status: str | None, enrollment_status: str | None) -> bool:
    """성적 draft는 수강생에게 노출하지 않는다."""
    return is_active_enrollment(enrollment_status) and grade_status == "published"
