import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from app.api.courses import list_courses
from app.schemas.course import CourseListResponse


COURSE_ONE = UUID("11111111-1111-1111-1111-111111111111")
COURSE_TWO = UUID("22222222-2222-2222-2222-222222222222")
USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, *, scalar=None, scalar_rows=None, rows=None):
        self._scalar = scalar
        self._scalar_rows = scalar_rows or []
        self._rows = rows or []

    def scalar(self):
        return self._scalar

    def scalars(self):
        return _ScalarRows(self._scalar_rows)

    def all(self):
        return self._rows


def _course(course_id: UUID, title: str, lesson_count: int):
    return SimpleNamespace(
        id=course_id,
        title=title,
        category="web",
        difficulty="basic",
        estimated_hours=2,
        thumbnail_url=None,
        is_published=True,
        is_deleted=False,
        status="approved",
        lessons=[object() for _ in range(lesson_count)],
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


class CourseListPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_progress_does_not_overwrite_filtered_total(self):
        courses = [_course(COURSE_ONE, "One", 2), _course(COURSE_TWO, "Two", 5)]
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    _Result(scalar=37),
                    _Result(scalar_rows=courses),
                    _Result(rows=[(COURSE_ONE, 2), (COURSE_TWO, 5)]),
                    _Result(rows=[(COURSE_ONE, 1), (COURSE_TWO, 4)]),
                ]
            )
        )

        response = await list_courses(
            category=None,
            difficulty=None,
            search=None,
            status_filter=None,
            page=2,
            size=2,
            include_pagination=True,
            include_deleted=False,
            db=db,
            user_id=USER_ID,
            user_role="user",
        )

        self.assertIsInstance(response, CourseListResponse)
        self.assertEqual(response.total, 37)
        self.assertEqual(response.page, 2)
        self.assertEqual(response.size, 2)
        self.assertEqual([item.progress_percent for item in response.items], [50.0, 80.0])
        self.assertEqual(db.execute.await_count, 4)

    async def test_legacy_default_still_returns_array_without_count_query(self):
        course = _course(COURSE_ONE, "Legacy", 1)
        db = SimpleNamespace(execute=AsyncMock(return_value=_Result(scalar_rows=[course])))

        response = await list_courses(
            category=None,
            difficulty=None,
            search=None,
            status_filter=None,
            page=1,
            size=20,
            include_pagination=False,
            include_deleted=False,
            db=db,
            user_id=None,
            user_role=None,
        )

        self.assertIsInstance(response, list)
        self.assertEqual([item.title for item in response], ["Legacy"])
        db.execute.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
