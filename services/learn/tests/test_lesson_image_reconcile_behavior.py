"""Behavior tests for the lesson image reconciliation grace and batch contract."""

from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.models.lesson_image import LessonImageAsset  # noqa: E402
from app.workers import lesson_image_reconcile_runner as reconcile_module  # noqa: E402


class _AsyncSessionAdapter:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._session = factory()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self._session.close()

    async def execute(self, statement):
        return self._session.execute(statement)

    async def commit(self) -> None:
        self._session.commit()

    async def rollback(self) -> None:
        self._session.rollback()


class _AsyncSessionFactory:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def __call__(self) -> _AsyncSessionAdapter:
        return _AsyncSessionAdapter(self._factory)


class LessonImageReconcileImportIsolationTests(unittest.TestCase):
    def test_import_preserves_an_existing_minio_module_and_its_exports(self) -> None:
        module_name = "app.utils.minio_client"
        original = sys.modules[module_name]
        self.assertEqual(original.MAX_FILE_SIZE, 100 * 1024 * 1024)
        sentinel = ModuleType(module_name)
        sentinel.MAX_FILE_SIZE = object()
        spec = importlib.util.spec_from_file_location("lesson_image_reconcile_import_probe", __file__)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        probe = importlib.util.module_from_spec(spec)

        with patch.dict(sys.modules, {module_name: sentinel}):
            spec.loader.exec_module(probe)
            self.assertIs(sys.modules[module_name], sentinel)
            self.assertIs(probe.reconcile_module, reconcile_module)
            self.assertIs(probe.reconcile_module.delete_lesson_image, reconcile_module.delete_lesson_image)

        self.assertIs(sys.modules[module_name], original)
        self.assertEqual(sys.modules[module_name].MAX_FILE_SIZE, 100 * 1024 * 1024)


class LessonImageReconcileBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        with self.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
            LessonImageAsset.__table__.create(connection)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.async_sessions = _AsyncSessionFactory(self.sessions)
        self.now = datetime(2026, 9, 8, 12, 0, 0)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _asset(
        self,
        index: int,
        *,
        state: str,
        created_at: datetime,
        cleanup_after: datetime | None = None,
        lease_until: datetime | None = None,
        last_error_code: str | None = None,
    ) -> LessonImageAsset:
        folder_id = f"{index:012x}"
        filename = f"{index:08x}.png"
        return LessonImageAsset(
            id=uuid.UUID(f"aaaaaaaa-aaaa-aaaa-aaaa-{index:012x}"),
            object_path=f"lesson-assets/images/{folder_id}/{filename}",
            folder_id=folder_id,
            filename=filename,
            original_name=f"image-{index}.png",
            state=state,
            created_at=created_at,
            cleanup_after=cleanup_after,
            lease_until=lease_until,
            attempt_count=0,
            last_error_code=last_error_code,
        )

    def _get(self, asset_id: uuid.UUID) -> LessonImageAsset:
        with self.sessions() as db:
            return db.execute(
                select(LessonImageAsset).where(LessonImageAsset.id == asset_id)
            ).scalar_one()

    async def test_grace_unknown_state_and_later_batch_candidate_are_preserved(self) -> None:
        referenced_one = self._asset(
            1,
            state="attached",
            created_at=self.now - timedelta(hours=10),
        )
        referenced_two = self._asset(
            2,
            state="attached",
            created_at=self.now - timedelta(hours=9),
        )
        old_staged = self._asset(
            3,
            state="staged",
            created_at=self.now - timedelta(hours=8),
        )
        due_orphan = self._asset(
            4,
            state="orphan_candidate",
            created_at=self.now - timedelta(hours=7),
            cleanup_after=self.now - timedelta(minutes=1),
        )
        fresh_staged = self._asset(
            5,
            state="staged",
            created_at=self.now - timedelta(minutes=30),
        )
        unknown = self._asset(
            6,
            state="future_state",
            created_at=self.now - timedelta(hours=6),
            last_error_code="future_state_reason",
        )
        with self.sessions() as db:
            db.add_all(
                [
                    referenced_one,
                    referenced_two,
                    old_staged,
                    due_orphan,
                    fresh_staged,
                    unknown,
                ]
            )
            db.commit()

        settings = reconcile_module.ReconcileSettings(
            staged_grace_seconds=60 * 60,
            orphan_grace_seconds=2 * 60 * 60,
            batch_size=1,
        )
        runner = reconcile_module.LessonImageReconcileRunner(settings)
        referenced = {referenced_one.object_path, referenced_two.object_path}

        with (
            patch.object(reconcile_module, "async_session", self.async_sessions),
            patch.object(reconcile_module, "utc_now", return_value=self.now),
        ):
            first_due, first_blocked = await runner._reconcile_rows(referenced)
            second_due, second_blocked = await runner._reconcile_rows(referenced)
            third_due, third_blocked = await runner._reconcile_rows(referenced)

        self.assertEqual(first_due, [])
        self.assertFalse(first_blocked)
        self.assertEqual(second_due, [])
        self.assertTrue(second_blocked)
        self.assertEqual(third_due, [])
        self.assertTrue(third_blocked)

        transitioned = self._get(old_staged.id)
        self.assertEqual(transitioned.state, "orphan_candidate")
        self.assertEqual(
            transitioned.cleanup_after,
            self.now + timedelta(hours=2),
        )

        blocked = self._get(due_orphan.id)
        self.assertEqual(blocked.state, "orphan_candidate")
        self.assertEqual(blocked.attempt_count, 0)
        self.assertIsNone(blocked.lease_until)
        self.assertEqual(blocked.last_error_code, "reference_scope_unknown")

        self.assertEqual(self._get(fresh_staged.id).state, "staged")
        unknown_after = self._get(unknown.id)
        self.assertEqual(unknown_after.state, "future_state")
        self.assertEqual(unknown_after.last_error_code, "future_state_reason")
        self.assertEqual(self._get(referenced_one.id).state, "attached")
        self.assertEqual(self._get(referenced_two.id).state, "attached")

    async def test_reference_scope_block_never_calls_minio_delete_or_reports_completion(self) -> None:
        blocked = self._asset(
            7,
            state="orphan_candidate",
            created_at=self.now - timedelta(hours=2),
            cleanup_after=self.now - timedelta(minutes=1),
        )
        with self.sessions() as db:
            db.add(blocked)
            db.commit()

        runner = reconcile_module.LessonImageReconcileRunner(
            reconcile_module.ReconcileSettings(batch_size=1)
        )
        with (
            patch.object(reconcile_module, "async_session", self.async_sessions),
            patch.object(reconcile_module, "utc_now", return_value=self.now),
            patch.object(runner, "_load_lesson_contents", AsyncMock(return_value=[])),
            patch.object(reconcile_module, "delete_lesson_image") as delete_image,
        ):
            result = await runner.run_once()

        self.assertEqual(result, "blocked_reference_scope")
        delete_image.assert_not_called()
        after = self._get(blocked.id)
        self.assertEqual(after.state, "orphan_candidate")
        self.assertEqual(after.last_error_code, "reference_scope_unknown")
        self.assertIsNone(after.deleted_at)

    async def test_run_logs_reference_scope_block_only_on_status_transitions(self) -> None:
        runner = reconcile_module.LessonImageReconcileRunner(
            reconcile_module.ReconcileSettings(poll_seconds=0.001)
        )
        statuses = iter(
            ["blocked_reference_scope", "blocked_reference_scope", "idle"]
        )

        async def run_once() -> str:
            status = next(statuses)
            if status == "idle":
                runner.request_shutdown()
            return status

        with (
            patch.object(runner, "_install_signal_handlers"),
            patch.object(runner, "run_once", side_effect=run_once),
            self.assertLogs(reconcile_module.logger, level="INFO") as captured,
        ):
            await runner.run()

        blocked_event = (
            "lesson_image_reconcile_runner.cleanup_blocked."
            "reference_scope_unknown"
        )
        cleared_event = "lesson_image_reconcile_runner.cleanup_block_cleared"
        messages = [record.getMessage() for record in captured.records]
        self.assertEqual(messages.count(blocked_event), 1)
        self.assertEqual(messages.count(cleared_event), 1)


if __name__ == "__main__":
    unittest.main()
