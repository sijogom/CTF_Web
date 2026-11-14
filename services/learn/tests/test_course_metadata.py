"""Focused contracts for optimistic course title/description editing."""

from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest

from pydantic import ValidationError


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.schemas.course import CourseMetadataUpdate  # noqa: E402
from app.utils.course_metadata import (  # noqa: E402
    course_metadata_revision,
    normalize_course_description_for_write,
)
from app.utils.rich_content import RichContentReviewRequired  # noqa: E402


def route_source(name: str) -> str:
    router = SERVICE_ROOT / "app" / "api" / "courses.py"
    source = router.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(router))
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"route not found: {name}")


class CourseMetadataRuleTests(unittest.TestCase):
    def test_revision_uses_compact_utf8_json_and_distinguishes_null_from_empty(self) -> None:
        self.assertEqual(
            course_metadata_revision("코스😀", "<p>설명</p>"),
            "38818483983bde3206c9cee1e591da09a9575d4d9cbcbd13f5e1146157d5bc65",
        )
        self.assertEqual(
            course_metadata_revision("Title", None),
            "f1bed9ef8cc854afa7ed15dc4b4bc711e2a4672564c673250b989cf19e709912",
        )
        self.assertNotEqual(
            course_metadata_revision("Title", None),
            course_metadata_revision("Title", ""),
        )

    def test_retained_legacy_description_is_normalized_without_changing_image_or_table_attributes(self) -> None:
        self.assertEqual(
            normalize_course_description_for_write("**굵게**"),
            "<p><strong>굵게</strong></p>",
        )
        source = (
            '<table><tbody><tr><td colspan="2" rowspan="1" align="center">값</td></tr></tbody></table>'
            '<p><img src="/api/learn/lesson-images/key/exact.png" alt="그림😀"></p>'
        )
        normalized = normalize_course_description_for_write(source)
        self.assertIn('colspan="2"', normalized)
        self.assertIn('rowspan="1"', normalized)
        self.assertIn('align="center"', normalized)
        self.assertIn('src="/api/learn/lesson-images/key/exact.png"', normalized)

    def test_ambiguous_retained_description_requires_review(self) -> None:
        with self.assertRaises(RichContentReviewRequired):
            normalize_course_description_for_write("#include <stdio.h>")

    def test_update_schema_is_sparse_and_rejects_null_title_or_unknown_fields(self) -> None:
        revision = "a" * 64
        sparse = CourseMetadataUpdate(expected_metadata_revision=revision, description=None)
        self.assertEqual(sparse.model_fields_set, {"expected_metadata_revision", "description"})
        with self.assertRaises(ValidationError):
            CourseMetadataUpdate(expected_metadata_revision=revision, title=None)
        with self.assertRaises(ValidationError):
            CourseMetadataUpdate(expected_metadata_revision=revision, category="web")


class CourseMetadataRouteContractTests(unittest.TestCase):
    def test_route_locks_before_revision_check_and_writes_only_metadata_audit(self) -> None:
        source = route_source("update_course_metadata")
        lock_index = source.index("with_for_update()")
        revision_index = source.index("before_revision =")
        self.assertLess(lock_index, revision_index)
        self.assertIn('require_role("admin", "moderator")', source)
        self.assertIn('action="course.metadata.update"', source)
        self.assertIn('"changed_fields": changed_fields', source)
        self.assertNotIn("category =", source)
        self.assertNotIn("is_published =", source)
        self.assertNotIn("status =", source)

    def test_course_detail_exposes_the_same_revision_function(self) -> None:
        source = route_source("get_course")
        self.assertIn(
            "metadata_revision=course_metadata_revision(course.title, course.description)",
            source,
        )


if __name__ == "__main__":
    unittest.main()
