"""AP1 additive031의 순수 metadata/DDL 회귀. 실제 PG16 upgrade·ORM은 별도다."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))
MIGRATION = SERVICE_ROOT / "migrations/versions/20261018_031_challenge_attachments.py"


def migration():
    spec = importlib.util.spec_from_file_location("ap1_additive031_pure", MIGRATION)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class AttachmentMigrationTests(unittest.TestCase):
    def test_revision_is_additive_after_current_030(self):
        value = migration()
        self.assertEqual(value.revision, "20261018_031")
        self.assertEqual(value.down_revision, "20261018_030")
        self.assertIsNone(value.branch_labels)
        self.assertIsNone(value.depends_on)

    def test_upgrade_adds_private_ledger_and_server_default_revision_without_dml(self):
        value = migration()
        operations = Mock()
        with patch.object(value, "op", operations):
            value.upgrade()
        tables = {call.args[0] for call in operations.create_table.call_args_list}
        self.assertEqual(tables, {"challenge_attachment_drafts", "challenge_attachments"})
        additions = [(call.args[0], call.args[1]) for call in operations.add_column.call_args_list]
        self.assertEqual(len(additions), 1)
        table, column = additions[0]
        self.assertEqual((table, column.name, column.nullable), ("challenges", "attachment_revision", False))
        self.assertEqual(str(column.server_default.arg), "0")
        self.assertFalse(operations.drop_column.called)
        self.assertFalse(operations.drop_table.called)
        for call in operations.execute.call_args_list:
            text = str(call.args[0]).upper()
            self.assertNotIn("UPDATE ", text)
            self.assertNotIn("DELETE ", text)
            self.assertNotIn("TRUNCATE ", text)

    def test_ledger_has_owner_uuid_original_bytes_hash_and_cas_without_ttl_policy(self):
        from app.models.challenge_attachment import ChallengeAttachment, ChallengeAttachmentDraft
        draft = ChallengeAttachmentDraft.__table__
        attachment = ChallengeAttachment.__table__
        self.assertTrue({"id", "owner_id", "challenge_id", "base_attachment_revision", "state"}.issubset(draft.columns.keys()))
        self.assertTrue({"id", "owner_id", "draft_id", "original_name", "byte_size", "sha256", "object_path", "state", "challenge_id"}.issubset(attachment.columns.keys()))
        self.assertEqual([column.name for column in attachment.primary_key.columns], ["id"])
        self.assertNotIn("expires_at", draft.columns)
        self.assertNotIn("expires_at", attachment.columns)
        self.assertNotIn("purge_after", attachment.columns)

    def test_original_challenge_files_json_and_legacy_columns_are_not_retyped(self):
        from sqlalchemy.dialects.postgresql import JSONB
        from app.models.challenge import Challenge
        self.assertIsInstance(Challenge.__table__.c.files.type, JSONB)
        self.assertFalse(Challenge.__table__.c.attachment_revision.nullable)
        self.assertEqual(str(Challenge.__table__.c.attachment_revision.server_default.arg), "0")
        node = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        source = ast.get_source_segment(MIGRATION.read_text(encoding="utf-8"), next(item for item in node.body if isinstance(item, ast.FunctionDef) and item.name == "upgrade"))
        self.assertNotIn("alter_column", source)
        self.assertNotIn('"files"', source)

    def test_destructive_downgrade_is_refused_without_losing_ledger(self):
        value = migration()
        operations = Mock()
        with patch.object(value, "op", operations), self.assertRaises(RuntimeError):
            value.downgrade()
        self.assertFalse(operations.drop_table.called)
        self.assertFalse(operations.drop_column.called)

    def test_runtime_and_explicit_migration_metadata_both_register_new_model(self):
        for relative in ("app/main.py", "migrations/env.py", "app/scripts/learn_migrate.py"):
            with self.subTest(relative=relative):
                self.assertIn("app.models.challenge_attachment", (SERVICE_ROOT / relative).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
