"""Pure and source-level contracts for the explicit Learn bootstrap job.

No SQLAlchemy, Alembic, database, Docker, or service is started here.  These
tests prove the allowed preflight branches, not a PostgreSQL migration run.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.utils.learn_schema_bootstrap_contract import (  # noqa: E402
    ALEMBIC_VERSION_TABLE,
    CORE_TABLES,
    EDUCATION_TABLES,
    LEGACY_TABLES,
    LearnSchemaPreparation,
    classify_learn_schema_tables,
)


LEARN_MIGRATE = SERVICE_ROOT / "app" / "scripts" / "learn_migrate.py"
MAIN = SERVICE_ROOT / "app" / "main.py"
ALEMBIC_ENV = SERVICE_ROOT / "migrations" / "env.py"
COMPOSE = SERVICE_ROOT.parents[1] / "infra" / "docker-compose.yml"


class LearnSchemaBootstrapClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application_tables = set(CORE_TABLES | EDUCATION_TABLES)

    def test_only_a_completely_empty_database_can_bootstrap(self) -> None:
        self.assertEqual(
            classify_learn_schema_tables(
                table_names=set(),
                application_table_names=self.application_tables,
            ),
            LearnSchemaPreparation.BOOTSTRAP_EMPTY,
        )

    def test_versioned_complete_core_can_take_upgrade_path(self) -> None:
        self.assertEqual(
            classify_learn_schema_tables(
                table_names=set(CORE_TABLES) | {ALEMBIC_VERSION_TABLE},
                application_table_names=self.application_tables,
            ),
            LearnSchemaPreparation.UPGRADE_EXISTING,
        )

    def test_future_additive_education_metadata_does_not_block_pre_upgrade_path(self) -> None:
        future_application_tables = self.application_tables | {"education_future_additive_table"}
        self.assertEqual(
            classify_learn_schema_tables(
                table_names=set(CORE_TABLES) | set(EDUCATION_TABLES) | {ALEMBIC_VERSION_TABLE},
                application_table_names=future_application_tables,
            ),
            LearnSchemaPreparation.UPGRADE_EXISTING,
        )

    def test_unversioned_legacy_schema_is_not_adopted_automatically(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "자동 채택"):
            classify_learn_schema_tables(
                table_names=set(CORE_TABLES),
                application_table_names=self.application_tables,
            )

    def test_approved_adoption_accepts_only_the_exact_known_legacy_shape(self) -> None:
        self.assertEqual(
            classify_learn_schema_tables(
                table_names=set(LEGACY_TABLES),
                application_table_names=self.application_tables | set(LEGACY_TABLES),
                allow_legacy_adoption=True,
            ),
            LearnSchemaPreparation.ADOPT_LEGACY,
        )
        with self.assertRaises(RuntimeError):
            classify_learn_schema_tables(
                table_names=set(LEGACY_TABLES) | {"unexpected_table"},
                application_table_names=self.application_tables | set(LEGACY_TABLES),
                allow_legacy_adoption=True,
            )

    def test_partial_core_partial_education_and_marker_only_fail_before_ddl(self) -> None:
        cases = (
            {"courses", ALEMBIC_VERSION_TABLE},
            set(CORE_TABLES) | {"education_role_grants", ALEMBIC_VERSION_TABLE},
            {ALEMBIC_VERSION_TABLE},
        )
        for table_names in cases:
            with self.subTest(table_names=table_names):
                with self.assertRaises(RuntimeError):
                    classify_learn_schema_tables(
                        table_names=table_names,
                        application_table_names=self.application_tables,
                    )

    def test_unknown_table_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "알 수 없는 사용자 테이블"):
            classify_learn_schema_tables(
                table_names=set(CORE_TABLES) | {ALEMBIC_VERSION_TABLE, "unrelated_table"},
                application_table_names=self.application_tables,
            )


class LearnSchemaBootstrapSourceContractTests(unittest.TestCase):
    def test_pristine_bootstrap_is_atomic_and_existing_path_is_upgrade_only(self) -> None:
        source = LEARN_MIGRATE.read_text(encoding="utf-8")
        self.assertIn("classify_learn_schema_tables", source)
        self.assertIn("await connection.run_sync(Base.metadata.create_all)", source)
        self.assertIn("await connection.run_sync(_verify_bootstrapped_metadata)", source)
        self.assertIn("await connection.run_sync(_stamp_bootstrapped_head)", source)
        self.assertIn('alembic_config.attributes["connection"] = connection', source)
        self.assertIn('command.stamp(alembic_config, revision)', source)
        self.assertIn('_stamp_revision(connection, "head")', source)
        self.assertIn('"upgrade", "head"', source)
        self.assertNotIn('"stamp", "head"]', source)
        self.assertIn("_verify_version_marker", source)
        self.assertIn("_verify_existing_education_table_group", source)
        self.assertIn("LEARN_SCHEMA_ADOPT_LEGACY", source)
        self.assertIn("LearnSchemaPreparation.ADOPT_LEGACY", source)
        self.assertIn("async def verify_upgraded_schema", source)
        self.assertIn("async def dispose_engine", source)
        self.assertIn("asyncio.run(prepare_and_dispose())", source)
        self.assertIn("asyncio.run(verify_and_dispose())", source)
        self.assertIn("await connection.run_sync(_verify_upgraded_metadata)", source)
        self.assertLess(source.index("_run_alembic(preparation)"), source.index("asyncio.run(verify_and_dispose())"))

    def test_pre_upgrade_education_guard_checks_only_partial_table_groups(self) -> None:
        source = LEARN_MIGRATE.read_text(encoding="utf-8")
        start = source.index("def _verify_existing_education_table_group")
        end = source.index("def _verify_upgraded_metadata", start)
        guard = source[start:end]

        self.assertIn("present == set(EDUCATION_TABLES)", guard)
        self.assertIn("부분 생성 상태", guard)
        self.assertNotIn("_metadata_schema_contract_missing", guard)

    def test_runtime_worker_no_longer_performs_schema_ddl(self) -> None:
        source = MAIN.read_text(encoding="utf-8")
        self.assertNotIn("Base.metadata.create_all", source)
        self.assertNotIn("ALTER TABLE courses", source)
        self.assertIn("app.scripts.learn_migrate", source)

    def test_alembic_env_reuses_explicit_bootstrap_connection(self) -> None:
        source = ALEMBIC_ENV.read_text(encoding="utf-8")
        self.assertIn('config.attributes.get("connection")', source)
        self.assertIn("do_run_migrations(supplied_connection)", source)

    def test_compose_uses_only_the_explicit_learn_migrate_job(self) -> None:
        source = COMPOSE.read_text(encoding="utf-8")
        self.assertIn('command: ["python", "-m", "app.scripts.learn_migrate"]', source)
        self.assertIn("partial/unversioned DB", source)
        self.assertIn("LEARN_SCHEMA_ADOPT_LEGACY", source)


if __name__ == "__main__":
    unittest.main()
