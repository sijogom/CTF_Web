from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.engine import make_url

from app.utils.rich_content_migration import (
    RichContentFieldSpec,
    RichContentMigrationError,
    _mutation_statement,
    database_target_fingerprint,
    file_sha256,
    load_reviewed_manifest,
    plan_value,
    write_manifest_exclusive,
)


metadata = MetaData()
documents = Table(
    "documents",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("content", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True)),
)


class RichContentMigrationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scalar_spec = RichContentFieldSpec(
            documents,
            "content",
            field_name="본문",
            guard_columns=("updated_at",),
        )

    def test_scalar_plan_preserves_original_and_hashes_conversion(self) -> None:
        plan = plan_value(self.scalar_spec, "기존 **본문**")
        self.assertEqual(plan["status"], "convertible")
        self.assertEqual(plan["before_value"], "기존 **본문**")
        self.assertEqual(plan["after_value"], "<p>기존 <strong>본문</strong></p>")
        self.assertNotEqual(plan["before_hash"], plan["after_hash"])

    def test_mapping_and_hint_list_are_atomic_on_one_ambiguous_component(self) -> None:
        mapping_spec = RichContentFieldSpec(
            documents,
            "content",
            kind="mapping",
            field_name="번역",
            allow_empty=True,
        )
        mapping = {"ko": "정상", "en": "문자열 <FLAG> 예제"}
        mapping_plan = plan_value(mapping_spec, mapping)
        self.assertEqual(mapping_plan["status"], "review_required")
        self.assertEqual(mapping_plan["after_value"], mapping)
        self.assertIn(
            "review_required",
            {component["status"] for component in mapping_plan["components"]},
        )

        hint_spec = RichContentFieldSpec(
            documents,
            "content",
            kind="hint_list",
            field_name="힌트",
        )
        hints = [
            {"content": "정상", "cost": 0},
            {"content": "#include <stdio.h>", "cost": 10},
        ]
        hint_plan = plan_value(hint_spec, hints)
        self.assertEqual(hint_plan["status"], "review_required")
        self.assertEqual(hint_plan["after_value"], hints)

    def test_database_fingerprint_omits_password_and_changes_with_database(self) -> None:
        first = SimpleNamespace(
            url=make_url("postgresql+asyncpg://user:secret-one@db:5432/alpha")
        )
        same_target = SimpleNamespace(
            url=make_url("postgresql+asyncpg://other:secret-two@db:5432/alpha")
        )
        other_database = SimpleNamespace(
            url=make_url("postgresql+asyncpg://user:secret-one@db:5432/beta")
        )
        first_fingerprint = database_target_fingerprint(first, "learn")
        self.assertEqual(
            first_fingerprint,
            database_target_fingerprint(same_target, "learn"),
        )
        self.assertNotEqual(
            first_fingerprint,
            database_target_fingerprint(other_database, "learn"),
        )
        self.assertNotIn("secret", first_fingerprint)

    def test_reviewed_manifest_requires_exact_hash_environment_and_target(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            manifest = {
                "manifest_version": 1,
                "service": "learn",
                "environment": "acceptance",
                "database_target_fingerprint": "f" * 64,
                "entries": [],
            }
            digest = write_manifest_exclusive(path, manifest)
            loaded = load_reviewed_manifest(
                path,
                expected_sha256=digest,
                service="learn",
                environment="acceptance",
                expected_environment="acceptance",
                target_fingerprint="f" * 64,
            )
            self.assertEqual(loaded["service"], "learn")
            with self.assertRaises(RichContentMigrationError):
                load_reviewed_manifest(
                    path,
                    expected_sha256=digest,
                    service="learn",
                    environment="acceptance",
                    expected_environment="acceptance",
                    target_fingerprint="e" * 64,
                )
            with self.assertRaises(FileExistsError):
                write_manifest_exclusive(path, manifest)
            self.assertEqual(file_sha256(path), digest)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), manifest)

    def test_raw_conditional_update_touches_only_the_content_column(self) -> None:
        compiled = str(_mutation_statement(self.scalar_spec, dialect()))
        self.assertIn("SET content = :replacement", compiled)
        self.assertIn("content IS NOT DISTINCT FROM :expected", compiled)
        self.assertNotIn("updated_at", compiled)


if __name__ == "__main__":
    unittest.main()

