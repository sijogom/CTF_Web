"""Source contract for course deletion-generation locking."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROUTER = Path(__file__).resolve().parents[1] / "app" / "api" / "courses.py"


def route_source(name: str) -> str:
    source = ROUTER.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(ROUTER))
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"route not found: {name}")


class CourseDeletionGenerationContractTests(unittest.TestCase):
    def test_confirmation_issuance_locks_parent_before_commit(self) -> None:
        source = route_source("create_course_deletion_confirmation")
        self.assertIn("select(Course).where(Course.id == course_id).with_for_update()", source)
        self.assertLess(source.index("with_for_update()"), source.index("db.add("))

    def test_restore_invalidates_unused_tokens_while_parent_is_locked(self) -> None:
        source = route_source("restore_course")
        self.assertIn("update(CourseDeletionConfirmation)", source)
        self.assertIn("CourseDeletionConfirmation.used_at.is_(None)", source)
        self.assertIn('"invalidated_confirmation_tokens"', source)
        self.assertLess(source.index("select(Course)"), source.index("update(CourseDeletionConfirmation)"))

    def test_delete_acquires_mutating_locks_in_parent_first_order(self) -> None:
        source = route_source("delete_course")
        parent_lock = source.index("select(Course).where(Course.id == course_id).with_for_update()")
        confirmation_lock = source.index(
            "select(CourseDeletionConfirmation)",
            source.index("confirmation_result ="),
        )
        self.assertLess(parent_lock, confirmation_lock)
        self.assertIn(".where(CourseDeletionConfirmation.id == confirmation_probe.id)", source)


if __name__ == "__main__":
    unittest.main()
