"""Source-only contracts for the Auth migration/bootstrap path.

These tests do not load runtime settings, connect to PostgreSQL, start Docker,
or read environment files.  They prove only the pure state/contract rules and
source wiring; actual Alembic/PostgreSQL execution remains a runtime gate.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
import sys
import unittest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SERVICE_ROOT.parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.utils.auth_schema_bootstrap_contract import (  # noqa: E402
    ADMIN_SETTINGS_TABLE,
    ALEMBIC_VERSION_TABLE,
    BAN_LEDGER_TABLES,
    CURRENT_AUTH_REQUIRED_COLUMNS,
    CURRENT_AUTH_REQUIRED_TABLES,
    LEGACY_CORE_REQUIRED_COLUMNS,
    LEGACY_CORE_TABLES,
    AuthSchemaPreparation,
    classify_auth_schema_tables,
    validate_current_auth_schema,
    validate_legacy_core_schema,
)


MIGRATIONS_ROOT = SERVICE_ROOT / "migrations" / "versions"
AUTH_MIGRATE = SERVICE_ROOT / "app" / "scripts" / "auth_migrate.py"
AUTH_MAIN = SERVICE_ROOT / "app" / "main.py"
ALEMBIC_ENV = SERVICE_ROOT / "migrations" / "env.py"
MIGRATIONS_README = SERVICE_ROOT / "migrations" / "README.md"
COMPOSE = PROJECT_ROOT / "infra" / "docker-compose.yml"


def _literal_assignment(source: str, name: str):
    tree = ast.parse(source)
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == name
            for target in statement.targets
        ):
            return ast.literal_eval(statement.value)
    raise AssertionError(f"{name} assignment was not found")


def _assigned_string_set(source: str, name: str) -> set[str]:
    tree = ast.parse(source)
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == name
            for target in statement.targets
        ):
            continue
        return set(ast.literal_eval(statement.value))
    raise AssertionError(f"{name} assignment was not found")


def _model_column_names(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in tree.body:
        if not isinstance(statement, ast.ClassDef) or statement.name != class_name:
            continue
        columns: set[str] = set()
        for member in statement.body:
            if not isinstance(member, ast.Assign) or not isinstance(member.value, ast.Call):
                continue
            if not isinstance(member.value.func, ast.Name) or member.value.func.id != "Column":
                continue
            for target in member.targets:
                if isinstance(target, ast.Name):
                    columns.add(target.id)
        return columns
    raise AssertionError(f"{class_name} was not found in {path}")


def _all_model_table_columns() -> dict[str, set[str]]:
    """Read model table/column declarations without importing runtime settings."""

    table_columns: dict[str, set[str]] = {}
    for model_path in (SERVICE_ROOT / "app" / "models").glob("*.py"):
        tree = ast.parse(model_path.read_text(encoding="utf-8"), filename=str(model_path))
        for statement in tree.body:
            if not isinstance(statement, ast.ClassDef):
                continue
            table_name: str | None = None
            columns: set[str] = set()
            for member in statement.body:
                if not isinstance(member, ast.Assign):
                    continue
                if any(
                    isinstance(target, ast.Name) and target.id == "__tablename__"
                    for target in member.targets
                ):
                    table_name = ast.literal_eval(member.value)
                    continue
                if not isinstance(member.value, ast.Call):
                    continue
                if not isinstance(member.value.func, ast.Name) or member.value.func.id != "Column":
                    continue
                for target in member.targets:
                    if isinstance(target, ast.Name):
                        columns.add(target.id)
            if table_name is not None:
                table_columns[table_name] = columns
    return table_columns


class AuthSchemaBootstrapClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application_tables = set(LEGACY_CORE_TABLES | BAN_LEDGER_TABLES)
        self.application_tables.update(
            {
                "inquiry_notification_receipts",
                "competition_deletion_confirmations",
            }
        )

    def test_only_table_free_database_is_pristine_bootstrap(self) -> None:
        self.assertEqual(
            classify_auth_schema_tables(
                table_names=set(),
                application_table_names=self.application_tables,
            ),
            AuthSchemaPreparation.BOOTSTRAP_EMPTY,
        )

    def test_complete_unversioned_legacy_core_is_explicitly_adoptable(self) -> None:
        self.assertEqual(
            classify_auth_schema_tables(
                table_names=set(LEGACY_CORE_TABLES) | {ADMIN_SETTINGS_TABLE},
                application_table_names=self.application_tables,
            ),
            AuthSchemaPreparation.ADOPT_COMPLETE_LEGACY,
        )

    def test_complete_versioned_core_uses_upgrade_without_bootstrap(self) -> None:
        self.assertEqual(
            classify_auth_schema_tables(
                table_names=set(LEGACY_CORE_TABLES) | {ALEMBIC_VERSION_TABLE},
                application_table_names=self.application_tables,
            ),
            AuthSchemaPreparation.UPGRADE_EXISTING,
        )

    def test_partial_core_marker_only_unknown_and_partial_ban_ledger_fail_closed(self) -> None:
        cases = (
            {"users"},
            {ALEMBIC_VERSION_TABLE},
            set(LEGACY_CORE_TABLES) | {"unexpected_table"},
            set(LEGACY_CORE_TABLES) | {"account_bans"},
        )
        for table_names in cases:
            with self.subTest(table_names=table_names):
                with self.assertRaises(RuntimeError):
                    classify_auth_schema_tables(
                        table_names=table_names,
                        application_table_names=self.application_tables,
                    )


class AuthSchemaContractTests(unittest.TestCase):
    def test_contract_columns_exist_in_registered_models_when_models_own_the_table(self) -> None:
        model_columns = _all_model_table_columns()
        for table_name, required_columns in LEGACY_CORE_REQUIRED_COLUMNS.items():
            with self.subTest(contract="legacy", table_name=table_name):
                self.assertIn(table_name, model_columns)
                self.assertTrue(required_columns <= model_columns[table_name])

        for table_name, required_columns in CURRENT_AUTH_REQUIRED_COLUMNS.items():
            if table_name == ADMIN_SETTINGS_TABLE:
                continue
            with self.subTest(contract="post_head", table_name=table_name):
                self.assertIn(table_name, model_columns)
                self.assertTrue(required_columns <= model_columns[table_name])

    def test_legacy_contract_accepts_only_revision_000_columns(self) -> None:
        legacy_schema = {
            table_name: set(column_names)
            for table_name, column_names in LEGACY_CORE_REQUIRED_COLUMNS.items()
        }
        validate_legacy_core_schema(legacy_schema)

        missing_baseline_column = deepcopy(legacy_schema)
        missing_baseline_column["users"].remove("email_verified")
        with self.assertRaisesRegex(RuntimeError, "users.email_verified"):
            validate_legacy_core_schema(missing_baseline_column)

    def test_post_head_contract_requires_current_runtime_fields(self) -> None:
        current_schema = {
            table_name: set(column_names)
            for table_name, column_names in CURRENT_AUTH_REQUIRED_COLUMNS.items()
        }
        self.assertEqual(set(current_schema), set(CURRENT_AUTH_REQUIRED_TABLES))
        validate_current_auth_schema(current_schema)

        missing_current_column = deepcopy(current_schema)
        missing_current_column["users"].remove("email_verify_code_hash")
        with self.assertRaisesRegex(RuntimeError, "users.email_verify_code_hash"):
            validate_current_auth_schema(missing_current_column)

        missing_current_table = deepcopy(current_schema)
        del missing_current_table["competition_deletion_confirmations"]
        with self.assertRaisesRegex(RuntimeError, "competition_deletion_confirmations"):
            validate_current_auth_schema(missing_current_table)


class AuthSchemaBootstrapSourceContractTests(unittest.TestCase):
    def test_legacy_path_validates_000_then_stamps_then_upgrades_then_post_verifies(self) -> None:
        source = AUTH_MIGRATE.read_text(encoding="utf-8")
        prepare_source = source.split("async def prepare_auth_schema", 1)[1].split(
            "def _run_alembic_upgrade", 1
        )[0]
        flow_source = source.split("async def run_auth_migration_flow", 1)[1].split(
            "def main()", 1
        )[0]
        main_source = source.split("def main()", 1)[1]

        self.assertIn("classify_auth_schema_tables", prepare_source)
        self.assertIn("AuthSchemaPreparation.BOOTSTRAP_EMPTY", prepare_source)
        self.assertIn("AuthSchemaPreparation.ADOPT_COMPLETE_LEGACY", prepare_source)
        self.assertIn("tables=_legacy_core_metadata_tables()", prepare_source)
        self.assertIn("_verify_legacy_core_contract", prepare_source)
        self.assertIn("_stamp_legacy_baseline", prepare_source)
        self.assertLess(
            prepare_source.index("_verify_legacy_core_contract"),
            prepare_source.index("_stamp_legacy_baseline"),
        )
        self.assertNotIn("_verify_current_schema_contract", prepare_source)
        self.assertNotIn("_verify_bootstrapped_metadata", source)
        self.assertIn('command.stamp(config, LEGACY_BASELINE_REVISION)', source)
        self.assertIn('LEGACY_BASELINE_REVISION = "20260813_000"', source)
        self.assertIn('"upgrade", "head"', source)
        self.assertLess(
            flow_source.index("_run_alembic_upgrade()"),
            flow_source.index("verify_auth_schema_post_upgrade()"),
        )
        self.assertIn("await engine.dispose()", flow_source)
        self.assertIn("asyncio.run(run_auth_migration_flow())", main_source)
        self.assertNotIn("learn_migrate", source)

    def test_versioned_database_uses_post_head_schema_verification(self) -> None:
        source = AUTH_MIGRATE.read_text(encoding="utf-8")
        self.assertIn("validate_current_auth_schema(_table_columns(connection))", source)
        self.assertIn("async def verify_auth_schema_post_upgrade", source)
        self.assertIn("await connection.run_sync(_verify_current_schema_contract)", source)

    def test_alembic_env_registers_all_current_auth_models(self) -> None:
        source = ALEMBIC_ENV.read_text(encoding="utf-8")
        self.assertIn('config.attributes.get("connection")', source)
        self.assertIn("do_run_migrations(supplied_connection)", source)
        self.assertIn("CompetitionDeletionConfirmation", source)
        self.assertIn("CompetitionSolve", source)
        self.assertIn("from app.models.notification import Notification", source)

    def test_compose_invokes_auth_wrapper_and_gates_auth_service(self) -> None:
        compose = COMPOSE.read_text(encoding="utf-8")
        auth_migrate = compose.split("  auth-migrate:\n", 1)[1].split(
            "  auth-service:\n", 1
        )[0]
        auth_service = compose.split("  auth-service:\n", 1)[1].split(
            "  # === 학습 서비스 ===", 1
        )[0]
        self.assertIn(
            'command: ["python", "-m", "app.scripts.auth_migrate"]', auth_migrate
        )
        self.assertNotIn("learn_migrate", auth_migrate)
        self.assertIn(
            "auth-migrate:\n        condition: service_completed_successfully",
            auth_service,
        )

    def test_application_startup_has_no_schema_ddl_bypass(self) -> None:
        source = AUTH_MAIN.read_text(encoding="utf-8")
        self.assertNotIn("Base.metadata.create_all", source)
        self.assertNotIn("ALTER TABLE", source)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS", source)
        self.assertIn("auth-migrate만 수행", source)

    def test_migration_readme_records_direct_development_limit(self) -> None:
        readme = MIGRATIONS_README.read_text(encoding="utf-8")
        self.assertIn("직접 개발", readme)
        self.assertIn("python -m app.scripts.auth_migrate", readme)
        self.assertIn("post-head", readme)

    def test_001_and_002_accept_the_current_ledger_model_columns(self) -> None:
        ban_migration = (
            MIGRATIONS_ROOT / "20260813_001_account_bans.py"
        ).read_text(encoding="utf-8")
        inquiry_migration = (
            MIGRATIONS_ROOT / "20260813_002_inquiry_notification_receipts.py"
        ).read_text(encoding="utf-8")

        account_ban_columns = _model_column_names(
            SERVICE_ROOT / "app" / "models" / "ban.py", "AccountBan"
        )
        ban_appeal_columns = _model_column_names(
            SERVICE_ROOT / "app" / "models" / "ban.py", "BanAppeal"
        )
        receipt_columns = _model_column_names(
            SERVICE_ROOT / "app" / "models" / "inquiry_notification.py",
            "InquiryNotificationReceipt",
        )

        self.assertTrue(
            _assigned_string_set(ban_migration, "_BAN_COLUMNS") <= account_ban_columns
        )
        self.assertTrue(
            _assigned_string_set(ban_migration, "_APPEAL_COLUMNS") <= ban_appeal_columns
        )
        self.assertTrue(
            _assigned_string_set(inquiry_migration, "_REQUIRED_COLUMNS") <= receipt_columns
        )

    def test_revision_chain_and_runtime_schema_migration_are_explicit(self) -> None:
        expected = {
            "20260813_000_legacy_baseline.py": ("20260813_000", None),
            "20260813_001_account_bans.py": ("20260813_001", "20260813_000"),
            "20260813_002_inquiry_notification_receipts.py": ("20260813_002", "20260813_001"),
            "20260813_003_legacy_runtime_schema_contract.py": ("20260813_003", "20260813_002"),
        }
        for filename, (revision, down_revision) in expected.items():
            with self.subTest(filename=filename):
                source = (MIGRATIONS_ROOT / filename).read_text(encoding="utf-8")
                self.assertEqual(_literal_assignment(source, "revision"), revision)
                self.assertEqual(_literal_assignment(source, "down_revision"), down_revision)

        source = (
            MIGRATIONS_ROOT / "20260813_003_legacy_runtime_schema_contract.py"
        ).read_text(encoding="utf-8")
        for contract_name in (
            "discord_id",
            "baekjoon_handle",
            "email_verify_code_required",
            "scoring_type",
            "is_deleted",
            "admin_settings",
            "competition_deletion_confirmations",
        ):
            self.assertIn(contract_name, source)
        self.assertIn("자동 downgrade 대신 검증된 백업 복원", source)


if __name__ == "__main__":
    unittest.main()
