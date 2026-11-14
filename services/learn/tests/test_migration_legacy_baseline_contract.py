"""레거시 create_all 기준선과 additive migration 경계를 정적으로 고정한다.

이 테스트는 Alembic, SQLAlchemy, PostgreSQL, Docker 또는 실제 DB를 import하거나
연결하지 않는다. migration source의 revision 체인과 fail-closed 분기를 표준 Python의
AST/문자열 계약으로만 검사한다. 따라서 통과는 실제 upgrade 실행 증거가 아니다.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_ROOT = SERVICE_ROOT / "migrations" / "versions"


MIGRATIONS = {
    "20260813_000": "20260813_000_legacy_baseline.py",
    "20260813_001": "20260813_001_knowledge_relation_ledger.py",
    "20260813_002": "20260813_002_course_difficulty_contract.py",
    "20260813_003": "20260813_003_challenge_environment_verification.py",
    "20260813_004": "20260813_004_environment_verification_failure_ledger.py",
    "20260813_005": "20260813_005_dynamic_instance_flags.py",
    "20260813_006": "20260813_006_knowledge_mapping_v2_core.py",
    "20260813_007": "20260813_007_knowledge_candidate_run_reproducibility.py",
    "20260813_008": "20260813_008_knowledge_candidate_scheduled_runner.py",
    "20260813_009": "20260813_009_platform_events.py",
    "20260813_010": "20260813_010_education_domain_v1.py",
    "20260813_011": "20260813_011_course_soft_delete_schema.py",
    "20260813_012": "20260813_012_knowledge_candidate_run_ledger_p1.py",
    "20260814_013": "20260814_013_submission_redaction_metadata.py",
    "20260814_014": "20260814_014_knowledge_event_assignment_nodes.py",
    "20260814_015": "20260814_015_education_asset_cleanup_jobs.py",
    "20260814_016": "20260814_016_lesson_image_asset_lifecycle.py",
    "20260814_017": "20260814_017_knowledge_tag_mapping_provenance.py",
    "20260814_018": "20260814_018_knowledge_tag_mapping_audit.py",
    "20260814_019": "20260814_019_knowledge_tag_revision.py",
    "20260814_020": "20260814_020_recommendation_consent_v1.py",
    "20260814_021": "20260814_021_knowledge_tag_candidate_approval.py",
    "20260814_022": "20260814_022_course_moderation_ledger.py",
    "20260814_023": "20260814_023_missing_runtime_indexes.py",
    "20260815_024": "20260815_024_challenge_submission_flag_encryption.py",
    "20260816_025": "20260816_025_submission_flag_reveal.py",
    "20260816_026": "20260816_026_challenge_image_cleanup_jobs.py",
    "20260816_027": "20260816_027_challenge_image_cleanup_lease_index.py",
}
_RUNTIME_MIGRATION_DEPS_AVAILABLE = (
    importlib.util.find_spec("alembic") is not None
    and importlib.util.find_spec("sqlalchemy") is not None
)


def _read_migration(revision: str) -> tuple[str, ast.Module]:
    source = (MIGRATIONS_ROOT / MIGRATIONS[revision]).read_text(encoding="utf-8")
    return source, ast.parse(source, filename=MIGRATIONS[revision])


def _literal_assignment(tree: ast.Module, name: str):
    for node in tree.body:
        targets = []
        value = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        if value is None:
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return ast.literal_eval(value)
    raise AssertionError(f"{name} assignment was not found")


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} function was not found")


def _function_source(source: str, function: ast.FunctionDef) -> str:
    lines = source.splitlines()
    return "\n".join(lines[function.lineno - 1 : function.end_lineno])


def _load_education_migration_module():
    path = MIGRATIONS_ROOT / MIGRATIONS["20260813_010"]
    spec = importlib.util.spec_from_file_location("education_domain_v1_contract", path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load education migration module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _EducationSchemaInspector:
    """Minimal Inspector fake for migration safety behavior, not a DB emulator."""

    def __init__(self, migration, *, tables: list[str] | None = None, missing_column: tuple[str, str] | None = None):
        self.migration = migration
        self.tables = tables if tables is not None else list(migration._TABLES)
        self.missing_column = missing_column

    def get_table_names(self):
        return self.tables

    def get_columns(self, table):
        columns = set(self.migration._REQUIRED_COLUMNS[table])
        if self.missing_column and self.missing_column[0] == table:
            columns.discard(self.missing_column[1])
        return [{"name": column} for column in columns]

    def get_indexes(self, table):
        return [{"name": name} for name in self.migration._REQUIRED_INDEXES[table]]

    def get_check_constraints(self, table):
        return [{"name": name} for name in self.migration._REQUIRED_CHECKS.get(table, ())]

    def get_pk_constraint(self, table):
        del table
        return {"constrained_columns": ["id"]}

    def get_unique_constraints(self, table):
        return [
            {"column_names": sorted(columns)}
            for columns in self.migration._REQUIRED_UNIQUE_COLUMNS.get(table, ())
        ]

    def get_foreign_keys(self, table):
        return [
            {"constrained_columns": [column], "referred_table": referred_table}
            for column, referred_table in self.migration._REQUIRED_FOREIGN_KEYS.get(table, ())
        ]


class MigrationLegacyBaselineContractTests(unittest.TestCase):
    def test_all_migration_sources_parse_without_runtime_dependencies(self) -> None:
        for revision in MIGRATIONS:
            with self.subTest(revision=revision):
                source, tree = _read_migration(revision)
                self.assertTrue(source.strip())
                self.assertIsInstance(tree, ast.Module)

    def test_revision_chain_is_a_single_contiguous_path_from_000_to_027(self) -> None:
        expected_down_revisions = {
            "20260813_000": None,
            "20260813_001": "20260813_000",
            "20260813_002": "20260813_001",
            "20260813_003": "20260813_002",
            "20260813_004": "20260813_003",
            "20260813_005": "20260813_004",
            "20260813_006": "20260813_005",
            "20260813_007": "20260813_006",
            "20260813_008": "20260813_007",
            "20260813_009": "20260813_008",
            "20260813_010": "20260813_009",
            "20260813_011": "20260813_010",
            "20260813_012": "20260813_011",
            "20260814_013": "20260813_012",
            "20260814_014": "20260814_013",
            "20260814_015": "20260814_014",
            "20260814_016": "20260814_015",
            "20260814_017": "20260814_016",
            "20260814_018": "20260814_017",
            "20260814_019": "20260814_018",
            "20260814_020": "20260814_019",
            "20260814_021": "20260814_020",
            "20260814_022": "20260814_021",
            "20260814_023": "20260814_022",
            "20260815_024": "20260814_023",
            "20260816_025": "20260815_024",
            "20260816_026": "20260816_025",
            "20260816_027": "20260816_026",
        }
        for revision, expected_down_revision in expected_down_revisions.items():
            with self.subTest(revision=revision):
                _, tree = _read_migration(revision)
                self.assertEqual(_literal_assignment(tree, "revision"), revision)
                self.assertEqual(
                    _literal_assignment(tree, "down_revision"),
                    expected_down_revision,
                )

    def test_000_is_an_explicit_non_destructive_legacy_baseline(self) -> None:
        source, tree = _read_migration("20260813_000")
        self.assertNotIn("op.create_table", source)
        self.assertNotIn("op.drop_table", source)
        for function_name in ("upgrade", "downgrade"):
            function = _function(tree, function_name)
            self.assertEqual(len(function.body), 1)
            self.assertIsInstance(function.body[0], ast.Pass)

    def test_003_returns_before_mutation_when_legacy_core_is_absent(self) -> None:
        source, tree = _read_migration("20260813_003")
        upgrade_source = _function_source(source, _function(tree, "upgrade"))
        guard = 'if "challenges" not in tables:'
        self.assertIn(guard, upgrade_source)
        self.assertIn("return", upgrade_source)
        self.assertLess(upgrade_source.index(guard), upgrade_source.index("_add_challenge_columns_if_missing()"))

    def test_004_allows_noop_only_when_both_legacy_tables_are_absent(self) -> None:
        source, tree = _read_migration("20260813_004")
        upgrade = _function(tree, "upgrade")
        upgrade_source = _function_source(source, upgrade)
        empty_database_guard = 'if "challenges" not in table_names and TABLE_NAME not in table_names:'
        self.assertIn(empty_database_guard, upgrade_source)
        self.assertLess(
            upgrade_source.index(empty_database_guard),
            upgrade_source.index("column = _image_id_column()"),
        )
        self.assertIn("if column is None:", upgrade_source)
        self.assertIn("raise RuntimeError(", upgrade_source)

        returns = [node for node in ast.walk(upgrade) if isinstance(node, ast.Return)]
        self.assertEqual(
            len(returns),
            1,
            "004 must not silently return for a partial challenges/verification-ledger state",
        )

    def test_005_has_a_clean_database_noop_but_fails_closed_for_partial_core(self) -> None:
        source, tree = _read_migration("20260813_005")
        upgrade_source = _function_source(source, _function(tree, "upgrade"))
        clean_guard = 'if "challenges" not in tables:'
        partial_guard = "if SUBMISSIONS_TABLE not in tables:"
        self.assertIn(clean_guard, upgrade_source)
        self.assertIn(partial_guard, upgrade_source)
        self.assertIn("raise RuntimeError(", upgrade_source)
        self.assertLess(upgrade_source.index(clean_guard), upgrade_source.index(partial_guard))
        self.assertNotIn("Base.metadata.create_all", upgrade_source)
        self.assertNotIn("op.drop_table", upgrade_source)

    def test_006_to_008_extend_required_v2_contracts_without_table_drops(self) -> None:
        source_006, tree_006 = _read_migration("20260813_006")
        upgrade_006 = _function_source(source_006, _function(tree_006, "upgrade"))
        self.assertLess(
            upgrade_006.index('_require_columns(\n        "knowledge_content_nodes"'),
            upgrade_006.index('_create_content_profiles()'),
        )
        self.assertLess(
            upgrade_006.index('_require_columns(\n        "knowledge_relations"'),
            upgrade_006.index('_create_content_profiles()'),
        )
        for table_name, create_call in (
            ("knowledge_content_profiles", "_create_content_profiles()"),
            ("knowledge_candidate_runs", "_create_candidate_runs()"),
            ("knowledge_relation_proposals", "_create_proposals()"),
            ("knowledge_relation_proposal_evidence", "_create_proposal_evidence()"),
            ("knowledge_relation_proposal_reviews", "_create_proposal_reviews()"),
            ("knowledge_candidate_run_items", "_create_candidate_run_items()"),
            ("knowledge_projection_outbox", "_create_projection_outbox()"),
        ):
            self.assertIn(f'if "{table_name}" not in tables:', upgrade_006)
            self.assertIn(create_call, upgrade_006)
        self.assertNotIn("op.drop_table", upgrade_006)

        for revision, required_call in (
            ("20260813_007", "_require_v2_tables()"),
            ("20260813_008", "_require_tables()"),
        ):
            source, tree = _read_migration(revision)
            upgrade_source = _function_source(source, _function(tree, "upgrade"))
            self.assertIn(required_call, upgrade_source)
            self.assertNotIn("op.create_table", upgrade_source)
            self.assertNotIn("op.drop_table", upgrade_source)

    def test_009_is_pairwise_additive_and_fails_closed_for_partial_event_state(self) -> None:
        source, tree = _read_migration("20260813_009")
        upgrade_source = _function_source(source, _function(tree, "upgrade"))
        self.assertIn("if has_events and has_audits:", upgrade_source)
        self.assertIn("if has_events or has_audits:", upgrade_source)
        self.assertIn("raise RuntimeError(", upgrade_source)
        self.assertIn("op.create_table(\n        _EVENT_TABLE", upgrade_source)
        self.assertIn("op.create_table(\n        _AUDIT_TABLE", upgrade_source)
        self.assertNotIn("op.drop_table", upgrade_source)

    def test_010_checks_complete_education_state_and_legacy_core_before_create(self) -> None:
        source, tree = _read_migration("20260813_010")
        upgrade_source = _function_source(source, _function(tree, "upgrade"))
        self.assertIn("def _assert_complete_or_absent", source)
        self.assertIn("if not present:", source)
        self.assertIn("if present == set(_TABLES):", source)
        self.assertIn("raise RuntimeError(", source)
        self.assertIn('if _assert_complete_or_absent(action="upgrade"):', upgrade_source)
        core_guard = "missing_core = _LEGACY_BOOTSTRAP_CORE_TABLES - _table_names()"
        self.assertIn(core_guard, upgrade_source)
        self.assertIn("raise RuntimeError(", upgrade_source)
        self.assertNotIn("return\n\n    op.create_table(", upgrade_source)
        self.assertIn('"courses", "lessons", "challenges"', source)
        self.assertLess(upgrade_source.index(core_guard), upgrade_source.index("op.create_table("))
        self.assertIn("def _schema_contract_missing", source)
        for required_inspection in (
            "inspector.get_columns(table)",
            "inspector.get_indexes(table)",
            "inspector.get_check_constraints(table)",
            "inspector.get_pk_constraint(table)",
            "inspector.get_unique_constraints(table)",
            "inspector.get_foreign_keys(table)",
        ):
            self.assertIn(required_inspection, source)

    def test_011_moves_course_soft_delete_schema_out_of_runtime_startup(self) -> None:
        source, tree = _read_migration("20260813_011")
        upgrade_source = _function_source(source, _function(tree, "upgrade"))
        downgrade_source = _function_source(source, _function(tree, "downgrade"))
        for column_name in (
            "is_deleted",
            "deleted_at",
            "deleted_by",
            "deletion_reason",
            "published_before_deletion",
        ):
            self.assertIn(column_name, upgrade_source)
        self.assertIn('op.create_index(_INDEX, _TABLE, ["is_deleted"])', upgrade_source)
        self.assertIn("_require_course_table()", upgrade_source)
        self.assertIn("raise RuntimeError(", downgrade_source)

    def test_012_adds_candidate_ledger_metadata_without_rewriting_existing_rows(self) -> None:
        source, tree = _read_migration("20260813_012")
        upgrade_source = _function_source(source, _function(tree, "upgrade"))
        downgrade_source = _function_source(source, _function(tree, "downgrade"))
        self.assertIn('revision: str = "20260813_012"', source)
        self.assertIn('down_revision: Union[str, None] = "20260813_011"', source)
        self.assertIn("_SCHEMA_VERSION_COLUMN", upgrade_source)
        self.assertIn("_REQUESTED_MAX_COLUMN", upgrade_source)
        self.assertIn("input_snapshot_schema_version", source)
        self.assertIn("requested_max_candidates", source)
        self.assertIn("op.create_check_constraint", upgrade_source)
        self.assertNotIn("UPDATE knowledge_candidate_runs", upgrade_source)
        self.assertNotIn("op.drop_table", upgrade_source)
        self.assertIn("destructive downgrade", downgrade_source)
        self.assertIn("raise RuntimeError(", downgrade_source)

    def test_013_adds_redaction_metadata_without_rewriting_legacy_submission_rows(self) -> None:
        source, tree = _read_migration("20260814_013")
        upgrade_source = _function_source(source, _function(tree, "upgrade"))
        downgrade_source = _function_source(source, _function(tree, "downgrade"))
        self.assertIn('revision: str = "20260814_013"', source)
        self.assertIn('down_revision: Union[str, None] = "20260813_012"', source)
        for column_name in (
            "submitted_flag_length",
            "submitted_flag_fingerprint",
            "submitted_flag_storage_version",
        ):
            self.assertIn(column_name, source)
        self.assertIn("char_length(submitted_flag)", upgrade_source)
        self.assertIn("_LEGACY_VERSION", source)
        self.assertNotIn("UPDATE submissions SET submitted_flag", upgrade_source)
        self.assertIn("non_legacy", downgrade_source)
        self.assertIn("raise RuntimeError(", downgrade_source)

    def test_014_adds_event_and_assignment_node_types_without_rewriting_rows(self) -> None:
        source, tree = _read_migration("20260814_014")
        self.assertIn('revision: str = "20260814_014"', source)
        self.assertIn('down_revision: Union[str, None] = "20260814_013"', source)
        self.assertIn("'event', 'assignment'", source)
        self.assertIn("destructive downgrade", source)

    def test_024_adds_encrypted_challenge_submission_storage_without_rewriting_legacy_rows(self) -> None:
        source, tree = _read_migration("20260815_024")
        upgrade_source = _function_source(source, _function(tree, "upgrade"))
        downgrade_source = _function_source(source, _function(tree, "downgrade"))
        self.assertIn('revision: str = "20260815_024"', source)
        self.assertIn('down_revision: Union[str, None] = "20260814_023"', source)
        for column_name in (
            "flag_ciphertext",
            "flag_key_version",
            "flag_storage_version",
            "flag_value_length",
        ):
            self.assertIn(column_name, source)
        self.assertIn("char_length(flag_value)", upgrade_source)
        self.assertIn("_LEGACY_VERSION", source)
        self.assertNotIn("UPDATE challenge_submissions SET flag_value", upgrade_source)
        self.assertIn("non_legacy", downgrade_source)
        self.assertIn("raise RuntimeError(", downgrade_source)

    def test_025_adds_restricted_submission_flag_reveal_storage(self) -> None:
        source, tree = _read_migration("20260816_025")
        upgrade_source = _function_source(source, _function(tree, "upgrade"))
        downgrade_source = _function_source(source, _function(tree, "downgrade"))
        self.assertIn('revision: str = "20260816_025"', source)
        self.assertIn('down_revision: Union[str, None] = "20260815_024"', source)
        self.assertIn("submitted_flag_ciphertext", source)
        self.assertIn("submitted_flag_key_version", source)
        self.assertIn("submission_flag_reveal_audits", source)
        self.assertIn("op.add_column", upgrade_source)
        self.assertIn("op.create_table", upgrade_source)
        self.assertIn("SELECT COUNT(*)", downgrade_source)
        self.assertIn("_AUDIT_TABLE", downgrade_source)
        self.assertIn("raise RuntimeError(", downgrade_source)

    def test_026_and_027_add_challenge_image_cleanup_outbox_contract(self) -> None:
        source_026, tree_026 = _read_migration("20260816_026")
        self.assertIn('revision: str = "20260816_026"', source_026)
        self.assertIn('down_revision: Union[str, None] = "20260816_025"', source_026)
        self.assertIn("challenge_image_cleanup_jobs", source_026)
        self.assertIn('"challenges" not in set(_inspector().get_table_names())', source_026)
        self.assertIsNotNone(tree_026)

        source_027, tree_027 = _read_migration("20260816_027")
        self.assertIn('revision: str = "20260816_027"', source_027)
        self.assertIn('down_revision: Union[str, None] = "20260816_026"', source_027)
        self.assertIn("ix_challenge_image_cleanup_jobs_lease_until", source_027)
        self.assertIsNotNone(tree_027)

    @unittest.skipUnless(
        _RUNTIME_MIGRATION_DEPS_AVAILABLE,
        "Alembic/SQLAlchemy runtime dependencies are not installed for this source-only test environment",
    )
    def test_010_runtime_contract_rejects_clean_partial_and_incomplete_named_schema(self) -> None:
        migration = _load_education_migration_module()

        clean_inspector = _EducationSchemaInspector(migration, tables=[])
        with patch.object(migration.op, "get_bind", return_value=object()), patch.object(
            migration.sa, "inspect", return_value=clean_inspector
        ):
            self.assertFalse(migration._assert_complete_or_absent(action="upgrade"))

        partial_inspector = _EducationSchemaInspector(
            migration,
            tables=["education_role_grants"],
        )
        with patch.object(migration.op, "get_bind", return_value=object()), patch.object(
            migration.sa, "inspect", return_value=partial_inspector
        ), self.assertRaises(RuntimeError):
            migration._assert_complete_or_absent(action="upgrade")

        incomplete_inspector = _EducationSchemaInspector(
            migration,
            missing_column=("education_assets", "object_path"),
        )
        with patch.object(migration.op, "get_bind", return_value=object()), patch.object(
            migration.sa, "inspect", return_value=incomplete_inspector
        ), self.assertRaisesRegex(RuntimeError, "education_assets\\.column:object_path"):
            migration._assert_complete_or_absent(action="upgrade")

    @unittest.skipUnless(
        _RUNTIME_MIGRATION_DEPS_AVAILABLE,
        "Alembic/SQLAlchemy runtime dependencies are not installed for this source-only test environment",
    )
    def test_010_upgrade_rejects_a_clean_legacy_database_before_any_ddl(self) -> None:
        migration = _load_education_migration_module()
        with patch.object(migration, "_assert_complete_or_absent", return_value=False), patch.object(
            migration, "_table_names", return_value=set()
        ), self.assertRaises(RuntimeError):
            migration.upgrade()


if __name__ == "__main__":
    unittest.main()
