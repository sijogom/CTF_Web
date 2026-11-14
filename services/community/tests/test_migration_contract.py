"""외부 DB 없이 Community Alembic 배포 계약의 회귀를 막는다."""

import ast
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SERVICE_ROOT.parents[1]


def _read(relative_path: str) -> str:
    return (SERVICE_ROOT / relative_path).read_text(encoding="utf-8")


class CommunityMigrationContractTests(unittest.TestCase):
    def test_revision_chain_is_linear_and_has_a_safe_baseline(self) -> None:
        expected = [
            ("20260813_000_community_core_baseline.py", "20260813_000", None),
            ("20260813_001_legacy_posts_contract.py", "20260813_001", "20260813_000"),
            ("20260813_002_moderation_ledger.py", "20260813_002", "20260813_001"),
            (
                "20260813_003_inquiry_ticket_delivery.py",
                "20260813_003",
                "20260813_002",
            ),
        ]

        for filename, revision, down_revision in expected:
            source = _read(f"migrations/versions/{filename}")
            self.assertIn(f'revision: str = "{revision}"', source)
            if down_revision is None:
                self.assertIn("down_revision: Union[str, None] = None", source)
            else:
                self.assertIn(f'down_revision: Union[str, None] = "{down_revision}"', source)

    def test_service_does_not_mutate_schema_at_startup(self) -> None:
        source = _read("app/main.py")
        self.assertNotIn("Base.metadata.create_all", source)
        self.assertNotIn("ALTER TABLE", source)
        self.assertIn("await engine.dispose()", source)

    def test_image_and_compose_run_migration_before_service(self) -> None:
        dockerfile = _read("Dockerfile")
        compose = (REPOSITORY_ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
        production = (REPOSITORY_ROOT / "infra" / "docker-compose.prod.yml").read_text(encoding="utf-8")

        self.assertIn("COPY alembic.ini ./", dockerfile)
        self.assertIn("COPY migrations/ ./migrations/", dockerfile)
        self.assertIn("community-migrate:", compose)
        self.assertIn('"alembic", "-c", "/app/alembic.ini", "upgrade", "head"', compose)
        self.assertIn(
            "community-migrate:\n        condition: service_completed_successfully",
            compose,
        )
        self.assertIn("community-migrate:", production)

    def test_migration_files_are_valid_python(self) -> None:
        for migration in sorted((SERVICE_ROOT / "migrations" / "versions").glob("*.py")):
            ast.parse(migration.read_text(encoding="utf-8"), filename=str(migration))


if __name__ == "__main__":
    unittest.main()
