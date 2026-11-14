"""Private education asset cleanup outbox worker.

The API transaction only soft-deletes the metadata and enqueues a durable job.
This worker is the sole public-code path that removes the private MinIO object.
Object paths and storage exception text are intentionally never written to logs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, or_, select

from app.database import async_session
from app.models.education import EducationAssetCleanupJob
from app.utils.education_visibility import utc_now
from app.utils.minio_client import delete_education_asset


logger = logging.getLogger(__name__)

_STATUS_PENDING = "pending"
_STATUS_PROCESSING = "processing"
_STATUS_COMPLETED = "completed"
_STATUS_FAILED = "failed"
_MAX_BACKOFF_SECONDS = 15 * 60


@dataclass(frozen=True)
class CleanupSettings:
    enabled: bool = True
    poll_seconds: float = 5.0
    lease_seconds: int = 60
    max_attempts: int = 10


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value < minimum:
        raise ValueError(f"{name} is below the minimum")
    return value


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} is below the minimum")
    return value


def settings_from_environment() -> CleanupSettings:
    return CleanupSettings(
        enabled=_env_bool("EDUCATION_ASSET_CLEANUP_ENABLED", True),
        poll_seconds=_env_float("EDUCATION_ASSET_CLEANUP_POLL_SECONDS", 5.0, minimum=0.25),
        lease_seconds=_env_int("EDUCATION_ASSET_CLEANUP_LEASE_SECONDS", 60, minimum=5),
        max_attempts=_env_int("EDUCATION_ASSET_CLEANUP_MAX_ATTEMPTS", 10, minimum=1),
    )


def _safe_error_code(error: BaseException) -> str:
    if isinstance(error, ValueError):
        return "invalid_object_path"
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return "storage_unavailable"
    return "unexpected_storage_error"


class EducationAssetCleanupRunner:
    def __init__(self, settings: CleanupSettings) -> None:
        self._settings = settings
        self._stop_event = asyncio.Event()

    def request_shutdown(self) -> None:
        if not self._stop_event.is_set():
            self._stop_event.set()
            logger.info("education_asset_cleanup_runner.shutdown_requested")

    async def run(self) -> None:
        self._install_signal_handlers()
        if not self._settings.enabled:
            logger.info("education_asset_cleanup_runner.disabled")
            return
        logger.info("education_asset_cleanup_runner.started")
        try:
            while not self._stop_event.is_set():
                await self.run_once()
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._settings.poll_seconds,
                    )
                except TimeoutError:
                    continue
        finally:
            logger.info("education_asset_cleanup_runner.stopped")

    async def run_once(self) -> str:
        claimed = await self._claim_one()
        if claimed is None:
            return "idle"
        job_id, object_path, attempt_count = claimed
        try:
            await asyncio.to_thread(delete_education_asset, object_path)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = _safe_error_code(error)
            await self._finish_failure(job_id, attempt_count, code)
            logger.warning(
                "education_asset_cleanup_runner.failed job_id=%s code=%s attempt=%d",
                job_id,
                code,
                attempt_count,
            )
            return "failed"
        await self._finish_success(job_id)
        logger.info("education_asset_cleanup_runner.completed job_id=%s", job_id)
        return "completed"

    async def _claim_one(self) -> tuple[object, str, int] | None:
        now = utc_now()
        async with async_session() as db:
            result = await db.execute(
                select(EducationAssetCleanupJob)
                .where(
                    or_(
                        and_(
                            EducationAssetCleanupJob.status == _STATUS_PENDING,
                            EducationAssetCleanupJob.available_at <= now,
                        ),
                        and_(
                            EducationAssetCleanupJob.status == _STATUS_PROCESSING,
                            or_(
                                EducationAssetCleanupJob.lease_until.is_(None),
                                EducationAssetCleanupJob.lease_until <= now,
                            ),
                        ),
                    )
                )
                .order_by(EducationAssetCleanupJob.available_at, EducationAssetCleanupJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            job = result.scalar_one_or_none()
            if job is None:
                await db.rollback()
                return None
            job.status = _STATUS_PROCESSING
            job.attempt_count += 1
            job.lease_until = now + timedelta(seconds=self._settings.lease_seconds)
            await db.commit()
            return job.id, job.object_path, job.attempt_count

    async def _finish_success(self, job_id: object) -> None:
        async with async_session() as db:
            job = (
                await db.execute(
                    select(EducationAssetCleanupJob)
                    .where(EducationAssetCleanupJob.id == job_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                await db.rollback()
                return
            now = utc_now()
            job.status = _STATUS_COMPLETED
            job.completed_at = now
            job.lease_until = None
            job.last_error_code = None
            await db.commit()

    async def _finish_failure(self, job_id: object, attempt_count: int, code: str) -> None:
        async with async_session() as db:
            job = (
                await db.execute(
                    select(EducationAssetCleanupJob)
                    .where(EducationAssetCleanupJob.id == job_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                await db.rollback()
                return
            now = utc_now()
            job.lease_until = None
            job.last_error_code = code
            if attempt_count >= self._settings.max_attempts:
                job.status = _STATUS_FAILED
            else:
                job.status = _STATUS_PENDING
                delay = min(2 ** max(attempt_count - 1, 0), _MAX_BACKOFF_SECONDS)
                job.available_at = now + timedelta(seconds=delay)
            await db.commit()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.request_shutdown)
            except (NotImplementedError, RuntimeError):
                continue


async def run_from_environment() -> None:
    await EducationAssetCleanupRunner(settings_from_environment()).run()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_from_environment())
    except ValueError:
        logger.error("education_asset_cleanup_runner.configuration_invalid")
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        raise SystemExit(0) from None
    except Exception:
        logger.error("education_asset_cleanup_runner.stopped_unexpectedly")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
