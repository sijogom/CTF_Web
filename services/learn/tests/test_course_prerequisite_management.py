"""Focused contracts for course prerequisite management.

The suite keeps database and deployment state out of scope.  It verifies the
pure graph/access decisions plus the API source boundaries that must preserve
the single authoritative CoursePrerequisite write path.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import unittest
from uuid import UUID, uuid4

from pydantic import ValidationError


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.schemas.course import (  # noqa: E402
    CoursePrerequisiteAccess,
    CoursePrerequisiteReplace,
)
from app.utils.course_prerequisite_rules import (  # noqa: E402
    build_course_prerequisite_access,
    is_public_course,
    would_create_course_prerequisite_cycle,
)


@dataclass
class _CourseState:
    is_published: bool = True
    is_deleted: bool = False
    status: str = "approved"


def _course(**overrides: object) -> _CourseState:
    values: dict[str, object] = {
        "is_published": True,
        "is_deleted": False,
        "status": "approved",
    }
    values.update(overrides)
    return _CourseState(**values)


def _detail(course_id: UUID, *, completed: bool) -> dict:
    return {
        "id": str(course_id),
        "title": "선행 코스",
        "completed": completed,
        "completed_lessons": 4 if completed else 2,
        "total_lessons": 4,
    }


class CoursePrerequisitePureRuleTests(unittest.TestCase):
    def test_only_approved_published_and_non_archived_courses_are_selectable(self) -> None:
        self.assertTrue(is_public_course(_course()))
        self.assertFalse(is_public_course(_course(is_published=False)))
        self.assertFalse(is_public_course(_course(is_deleted=True)))
        self.assertFalse(is_public_course(_course(status="pending")))

    def test_cycle_detection_follows_course_to_prerequisite_direction(self) -> None:
        course_a, course_b, course_c = uuid4(), uuid4(), uuid4()
        self.assertTrue(
            would_create_course_prerequisite_cycle(
                course_id=course_a,
                prerequisite_ids=[course_b],
                existing_edges=[(course_b, course_c), (course_c, course_a)],
            )
        )
        self.assertFalse(
            would_create_course_prerequisite_cycle(
                course_id=course_a,
                prerequisite_ids=[course_b],
                existing_edges=[(course_b, course_c)],
            )
        )

    def test_partial_progress_is_locked_and_points_to_first_incomplete_course(self) -> None:
        target_id, completed_id, incomplete_id = uuid4(), uuid4(), uuid4()
        access = build_course_prerequisite_access(
            course_id=target_id,
            is_authenticated=True,
            locked=True,
            prerequisite_details=[
                _detail(completed_id, completed=True),
                _detail(incomplete_id, completed=False),
            ],
        )
        parsed = CoursePrerequisiteAccess.model_validate(access)
        self.assertEqual(parsed.access_state, "locked")
        self.assertEqual(parsed.reason_code, "prerequisites_incomplete")
        self.assertEqual(parsed.next_action.kind, "open_prerequisite_course")
        self.assertEqual(parsed.next_action.href, f"/learn/{incomplete_id}")

    def test_anonymous_user_gets_a_sign_in_action_without_progress_disclosure(self) -> None:
        target_id = uuid4()
        access = build_course_prerequisite_access(
            course_id=target_id,
            is_authenticated=False,
            locked=True,
            prerequisite_details=[_detail(uuid4(), completed=False)],
        )
        parsed = CoursePrerequisiteAccess.model_validate(access)
        self.assertEqual(parsed.access_state, "sign_in_required")
        self.assertEqual(parsed.reason_code, "authentication_required")
        self.assertEqual(parsed.next_action.href, f"/login?next=/learn/{target_id}")

    def test_completed_or_absent_requirements_open_the_target_course(self) -> None:
        target_id = uuid4()
        access = build_course_prerequisite_access(
            course_id=target_id,
            is_authenticated=True,
            locked=False,
            prerequisite_details=[_detail(uuid4(), completed=True)],
        )
        parsed = CoursePrerequisiteAccess.model_validate(access)
        self.assertEqual(parsed.access_state, "available")
        self.assertEqual(parsed.next_action.kind, "open_course")

    def test_schema_rejects_duplicate_prerequisite_ids(self) -> None:
        duplicate_id = uuid4()
        with self.assertRaises(ValidationError):
            CoursePrerequisiteReplace(prerequisite_ids=[duplicate_id, duplicate_id])


class CoursePrerequisiteSourceContractTests(unittest.TestCase):
    def test_management_endpoint_keeps_authorization_validation_and_atomic_replace_boundaries(self) -> None:
        source = (SERVICE_ROOT / "app" / "api" / "courses.py").read_text(encoding="utf-8")

        self.assertIn('"/courses/{course_id}/prerequisites"', source)
        self.assertIn('require_role("admin", "moderator")', source)
        self.assertIn("_validate_course_prerequisite_replace", source)
        self.assertIn("with_for_update()", source)
        self.assertIn("is_public_course(target)", source)
        self.assertIn("would_create_course_prerequisite_cycle", source)
        self.assertIn("delete(CoursePrerequisite)", source)
        self.assertIn('action="course.prerequisites.replace"', source)
        self.assertIn("await db.commit()", source)

    def test_course_detail_and_direct_learning_routes_share_the_same_server_lock_check(self) -> None:
        courses_source = (SERVICE_ROOT / "app" / "api" / "courses.py").read_text(encoding="utf-8")
        lessons_source = (SERVICE_ROOT / "app" / "api" / "lessons.py").read_text(encoding="utf-8")
        progress_source = (SERVICE_ROOT / "app" / "api" / "progress.py").read_text(encoding="utf-8")

        self.assertIn("get_course_prerequisite_status", courses_source)
        self.assertIn("build_course_prerequisite_access", courses_source)
        self.assertIn("get_course_prerequisite_status", lessons_source)
        self.assertIn("get_course_prerequisite_status", progress_source)
        self.assertNotIn("KnowledgeRelation", courses_source)


if __name__ == "__main__":
    unittest.main()
