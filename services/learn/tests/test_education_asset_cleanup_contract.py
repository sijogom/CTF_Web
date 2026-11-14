"""Source-only contracts for the education asset cleanup outbox."""

from __future__ import annotations

from pathlib import Path
import unittest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVICE_ROOT.parents[1]


class EducationAssetCleanupContractTests(unittest.TestCase):
    def test_model_and_migration_are_durable_and_non_destructive(self) -> None:
        model = (SERVICE_ROOT / "app" / "models" / "education.py").read_text(encoding="utf-8")
        migration = next(
            (SERVICE_ROOT / "migrations" / "versions").glob(
                "20260814_015_education_asset_cleanup_jobs.py"
            )
        ).read_text(encoding="utf-8")
        self.assertIn("class EducationAssetCleanupJob", model)
        self.assertIn('__tablename__ = "education_asset_cleanup_jobs"', model)
        for status in ("pending", "processing", "completed", "failed"):
            self.assertIn(status, model)
        self.assertIn('revision: str = "20260814_015"', migration)
        self.assertIn('down_revision: Union[str, None] = "20260814_014"', migration)
        self.assertIn("op.create_table", migration)
        self.assertIn("SELECT COUNT(*)", migration)
        self.assertIn("destructive downgrade", migration)

    def test_delete_endpoint_only_enqueues_after_soft_delete(self) -> None:
        source = (SERVICE_ROOT / "app" / "api" / "education.py").read_text(encoding="utf-8")
        start = source.index('async def delete_education_asset_endpoint')
        end = source.index('@router.get("/assets/{asset_id}/download")', start)
        endpoint = source[start:end]
        self.assertIn("EducationAssetCleanupJob(", endpoint)
        self.assertIn('status="pending"', endpoint)
        self.assertIn('"storage_cleanup_job_queued": True', endpoint)
        self.assertIn('"storage_cleanup_pending": True', endpoint)
        self.assertNotIn("delete_education_asset(asset.object_path)", endpoint)
        self.assertNotIn("get_minio_client", endpoint)

    def test_worker_claims_with_lease_and_uses_safe_error_codes(self) -> None:
        worker = (
            SERVICE_ROOT / "app" / "workers" / "education_asset_cleanup_runner.py"
        ).read_text(encoding="utf-8")
        self.assertIn("skip_locked=True", worker)
        self.assertIn("lease_until", worker)
        self.assertIn("attempt_count", worker)
        for code in ("invalid_object_path", "storage_unavailable", "unexpected_storage_error"):
            self.assertIn(code, worker)
        self.assertNotIn("logger.error(.*object_path", worker)
        self.assertNotIn("logger.warning(.*object_path", worker)

    def test_compose_has_private_worker_without_public_ports(self) -> None:
        base = (REPO_ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
        prod = (REPO_ROOT / "infra" / "docker-compose.prod.yml").read_text(encoding="utf-8")
        base_start = base.index("  education-asset-cleanup-runner:")
        base_end = base.index("  community-migrate:", base_start)
        base_service = base[base_start:base_end]
        self.assertIn("app.workers.education_asset_cleanup_runner", base_service)
        self.assertIn("learn-migrate:", base_service)
        self.assertIn("learn-service:", base_service)
        self.assertNotIn("    ports:", base_service)
        prod_start = prod.index("  education-asset-cleanup-runner:")
        prod_end = prod.index("  community-service:", prod_start)
        prod_service = prod[prod_start:prod_end]
        self.assertIn("no-new-privileges:true", prod_service)
        self.assertIn("read_only: true", prod_service)
        self.assertIn("cap_drop:", prod_service)


if __name__ == "__main__":
    unittest.main()
