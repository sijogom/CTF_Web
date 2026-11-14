"""Source contract for competition deletion-generation locking."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROUTER = Path(__file__).resolve().parents[1] / "app" / "api" / "competition.py"


def route_source(name: str) -> str:
    source = ROUTER.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(ROUTER))
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"route not found: {name}")


class CompetitionDeletionGenerationContractTests(unittest.TestCase):
    def test_confirmation_issuance_locks_parent_before_commit(self) -> None:
        source = route_source("create_competition_deletion_confirmation")
        self.assertIn("select(Competition).where(Competition.id == comp_id).with_for_update()", source)
        self.assertLess(source.index("with_for_update()"), source.index("db.add("))

    def test_restore_invalidates_unused_tokens_while_parent_is_locked(self) -> None:
        source = route_source("restore_competition")
        self.assertIn("update(CompetitionDeletionConfirmation)", source)
        self.assertIn("CompetitionDeletionConfirmation.used_at.is_(None)", source)
        self.assertIn('"invalidated_confirmation_tokens"', source)
        self.assertLess(source.index("select(Competition)"), source.index("update(CompetitionDeletionConfirmation)"))

    def test_delete_acquires_mutating_locks_in_parent_first_order(self) -> None:
        source = route_source("delete_competition")
        parent_lock = source.index("select(Competition).where(Competition.id == comp_id).with_for_update()")
        confirmation_lock = source.index(
            "select(CompetitionDeletionConfirmation)",
            source.index("confirmation_result ="),
        )
        self.assertLess(parent_lock, confirmation_lock)
        self.assertIn(".where(CompetitionDeletionConfirmation.id == confirmation_probe.id)", source)


if __name__ == "__main__":
    unittest.main()
