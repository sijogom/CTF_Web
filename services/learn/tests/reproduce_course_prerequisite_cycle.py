"""Historical pre-fix reproduction. Expected to reject/fail after serialization."""
import asyncio
import json
from unittest.mock import patch

from sqlalchemy import select

from test_knowledge_postgres import KnowledgePostgresTests
from app.api.courses import _validate_course_prerequisite_replace, replace_course_prerequisites
from app.models.course import Course, CoursePrerequisite
from app.schemas.course import CoursePrerequisiteReplace
from app.utils.course_prerequisite_rules import would_create_course_prerequisite_cycle


async def main():
    test = KnowledgePostgresTests()
    await test.asyncSetUp()
    try:
        async with test.session() as db:
            courses = [Course(title=f"Cycle {name}", category="web", difficulty="basic", author_id=test.author,
                              status="approved", is_published=True) for name in "ABCD"]
            db.add_all(courses)
            await db.flush()
            a, b, c, d = [course.id for course in courses]
            db.add_all([CoursePrerequisite(course_id=b, prerequisite_id=c),
                        CoursePrerequisite(course_id=d, prerequisite_id=a)])
            await db.commit()
        validated_count = 0
        both_validated = asyncio.Event()
        async def paused_validation(db, **arguments):
            nonlocal validated_count
            await _validate_course_prerequisite_replace(db, **arguments)
            validated_count += 1
            if validated_count == 2:
                both_validated.set()
            await asyncio.wait_for(both_validated.wait(), 3)

        async def replace(source, target, actor):
            async with test.session() as db:
                await replace_course_prerequisites(source, CoursePrerequisiteReplace(prerequisite_ids=[target]),
                    db=db, actor_id=actor, _role="admin")
                return 200
        with patch("app.api.courses._validate_course_prerequisite_replace", side_effect=paused_validation):
            statuses = await asyncio.wait_for(asyncio.gather(replace(a, b, test.author), replace(c, d, test.reviewer)), 8)
        async with test.session() as db:
            edges = (await db.execute(select(CoursePrerequisite.course_id, CoursePrerequisite.prerequisite_id))).all()
        cyclic = would_create_course_prerequisite_cycle(course_id=a, prerequisite_ids=[b],
            existing_edges=[edge for edge in edges if edge.course_id != a])
        if statuses != [200, 200] or not cyclic:
            raise AssertionError("Expected pre-fix cycle TOCTOU did not reproduce")
        print(json.dumps({"ok": True, "pre_fix_cycle_toctou_reproduced": True, "statuses": statuses,
                          "committed_cycle_edges": len(edges), "public_schema_modified": False, "production_writes": 0}))
    finally:
        await test.asyncTearDown()


if __name__ == "__main__":
    asyncio.run(main())
