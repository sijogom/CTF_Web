"""레슨 본문 이미지 staged/attached/orphan lifecycle source 계약."""

from pathlib import Path
import unittest


SERVICE_ROOT = Path(__file__).resolve().parents[1]


class LessonImageLifecycleSourceTests(unittest.TestCase):
    def test_model_migration_and_metadata_registration_are_present(self) -> None:
        model = (SERVICE_ROOT / "app" / "models" / "lesson_image.py").read_text(encoding="utf-8")
        migration = (
            SERVICE_ROOT
            / "migrations"
            / "versions"
            / "20260814_016_lesson_image_asset_lifecycle.py"
        ).read_text(encoding="utf-8")
        main = (SERVICE_ROOT / "app" / "main.py").read_text(encoding="utf-8")
        env = (SERVICE_ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
        self.assertIn('__tablename__ = "lesson_image_assets"', model)
        for state in ("staged", "attached", "orphan_candidate", "cleanup_pending", "deleted"):
            self.assertIn(state, model)
        self.assertIn('revision: str = "20260814_016"', migration)
        self.assertIn('down_revision: Union[str, None] = "20260814_015"', migration)
        self.assertIn("if _TABLE in tables:", migration)
        self.assertIn("partial schema를 자동 채택하지 않습니다", migration)
        self.assertIn("app.models.lesson_image", main)
        self.assertIn("app.models.lesson_image", env)

    def test_upload_records_asset_and_returns_safe_identifier(self) -> None:
        route = (SERVICE_ROOT / "app" / "api" / "lessons.py").read_text(encoding="utf-8")
        schema = (SERVICE_ROOT / "app" / "schemas" / "course.py").read_text(encoding="utf-8")
        self.assertIn("db: AsyncSession = Depends(get_db)", route)
        self.assertIn("LessonImageAsset(", route)
        self.assertIn("await db.commit()", route)
        self.assertIn("delete_lesson_image(result[\"object_path\"])", route)
        self.assertIn("asset_id: UUID", schema)
        self.assertNotIn("object_path: str", schema[schema.index("class ImageUploadResponse"):])

    def test_reconcile_worker_scans_content_and_retries_storage_failures(self) -> None:
        worker = (
            SERVICE_ROOT
            / "app"
            / "workers"
            / "lesson_image_reconcile_runner.py"
        ).read_text(encoding="utf-8")
        minio = (SERVICE_ROOT / "app" / "utils" / "minio_client.py").read_text(encoding="utf-8")
        self.assertIn("select(Lesson.content)", worker)
        self.assertIn("_referenced_object_paths", worker)
        self.assertIn("with_for_update(skip_locked=True)", worker)
        self.assertIn("delete_lesson_image", worker)
        self.assertIn("_mark_failure", worker)
        self.assertIn("_LESSON_IMAGE_OBJECT_PATH_PATTERN", minio)
        self.assertNotIn("logger.error(\"%s\", object_path", worker)

    def test_runner_is_private_and_ordered_after_learn_migration(self) -> None:
        compose = (SERVICE_ROOT.parents[1] / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("lesson-image-reconcile-runner:", compose)
        start = compose.index("lesson-image-reconcile-runner:")
        runner = compose[start : start + 1800]
        self.assertIn("app.workers.lesson_image_reconcile_runner", runner)
        self.assertIn("learn-migrate:", runner)
        self.assertIn("service_completed_successfully", runner)
        self.assertNotIn("ports:", runner)


if __name__ == "__main__":
    unittest.main()
