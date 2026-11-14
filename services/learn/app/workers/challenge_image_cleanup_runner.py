"""삭제된 문제의 소유 runtime 자원 정리를 재시도하는 durable outbox worker."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import httpx
from sqlalchemy import and_, or_, select

from app.config import settings
from app.database import async_session
from app.models.challenge_image_cleanup import ChallengeImageCleanupJob
from app.utils.education_visibility import utc_now


logger = logging.getLogger(__name__)

_STATUS_PENDING = "pending"
_STATUS_PROCESSING = "processing"
_STATUS_COMPLETED = "completed"
_STATUS_FAILED = "failed"
_MAX_BACKOFF_SECONDS = 15 * 60


class CleanupBoundaryError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CleanupSettings:
    enabled: bool = True
    poll_seconds: float = 5.0
    lease_seconds: int = 60
    max_attempts: int = 20


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
        enabled=_env_bool("CHALLENGE_IMAGE_CLEANUP_ENABLED", True),
        poll_seconds=_env_float(
            "CHALLENGE_IMAGE_CLEANUP_POLL_SECONDS", 5.0, minimum=0.25
        ),
        lease_seconds=_env_int(
            "CHALLENGE_IMAGE_CLEANUP_LEASE_SECONDS", 60, minimum=5
        ),
        max_attempts=_env_int(
            "CHALLENGE_IMAGE_CLEANUP_MAX_ATTEMPTS", 20, minimum=1
        ),
    )


def _safe_error_code(error: BaseException) -> str:
    if isinstance(error, CleanupBoundaryError):
        return error.code
    if isinstance(error, httpx.RequestError):
        return "env_orchestrator_unavailable"
    if isinstance(error, ValueError):
        return "invalid_cleanup_response"
    return "challenge_image_cleanup_failed"


def _validate_image_reference(challenge_id: int, image_reference: str) -> bool:
    return image_reference in {"", f"ctf-challenge-{challenge_id}:latest"}


class ChallengeImageCleanupRunner:
    def __init__(self, settings_value: CleanupSettings) -> None:
        self._settings = settings_value
        self._stop_event = asyncio.Event()

    def request_shutdown(self) -> None:
        if not self._stop_event.is_set():
            self._stop_event.set()
            logger.info("challenge_image_cleanup_runner.shutdown_requested")

    async def run(self) -> None:
        self._install_signal_handlers()
        if not self._settings.enabled:
            logger.info("challenge_image_cleanup_runner.disabled")
            return
        logger.info("challenge_image_cleanup_runner.started")
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
            logger.info("challenge_image_cleanup_runner.stopped")

    async def run_once(self) -> str:
        claimed = await self._claim_one()
        if claimed is None:
            return "idle"
        job_id, challenge_id, image_reference, attempt_count = claimed
        try:
            if not _validate_image_reference(challenge_id, image_reference):
                raise ValueError("image reference is outside the platform-owned tag contract")
            status = await self._request_cleanup(job_id, challenge_id, image_reference)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = _safe_error_code(error)
            if not await self._finish_failure(job_id, attempt_count, code):
                return "stale"
            logger.warning(
                "challenge_image_cleanup_runner.failed job_id=%s code=%s attempt=%d",
                job_id,
                code,
                attempt_count,
            )
            return "failed"
        if not await self._finish_success(job_id, attempt_count):
            return "stale"
        logger.info(
            "challenge_image_cleanup_runner.completed job_id=%s status=%s",
            job_id,
            status,
        )
        return "completed"

    async def _request_cleanup(self, job_id: object, challenge_id: int, image_reference: str) -> str:
        if not settings.env_orchestrator_url or not settings.internal_api_key:
            raise ValueError("cleanup boundary is not configured")
        url = (
            f"{settings.env_orchestrator_url.rstrip('/')}/internal/challenges/"
            f"{challenge_id}/cleanup-resources"
        )
        timeout = httpx.Timeout(8.0, connect=3.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"X-Internal-Key": settings.internal_api_key},
        ) as client:
            response = await client.post(url, json={"job_id": str(job_id)})
        if response.status_code not in {200, 409, 503}:
            raise ValueError("cleanup boundary returned an unexpected status")
        if response.status_code == 409:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            code = payload.get("code") if isinstance(payload, dict) else None
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if isinstance(detail, dict):
                code = detail.get("code")
            raise CleanupBoundaryError(
                "cleanup_challenge_exists" if code == "cleanup_challenge_exists" else "cleanup_target_rejected"
            )
        if response.status_code == 503:
            raise CleanupBoundaryError("env_orchestrator_unavailable")
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("cleanup boundary response is not an object")
            returned_job_id = UUID(payload.get("job_id", ""))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("cleanup boundary response is invalid") from error
        if (
            returned_job_id != UUID(str(job_id))
            or type(payload.get("challenge_id")) is not int
            or payload["challenge_id"] != challenge_id
            or payload.get("image_reference") != image_reference
        ):
            raise ValueError("cleanup boundary target does not match the claimed job")
        status = payload.get("status")
        if status not in {"removed", "absent"}:
            raise ValueError("cleanup boundary status is invalid")
        return status

    async def _claim_one(self) -> tuple[object, int, str, int] | None:
        now = utc_now()
        async with async_session() as db:
            result = await db.execute(
                select(ChallengeImageCleanupJob)
                .where(
                    or_(
                        and_(
                            ChallengeImageCleanupJob.status == _STATUS_PENDING,
                            ChallengeImageCleanupJob.available_at <= now,
                        ),
                        and_(
                            ChallengeImageCleanupJob.status == _STATUS_PROCESSING,
                            or_(
                                ChallengeImageCleanupJob.lease_until.is_(None),
                                ChallengeImageCleanupJob.lease_until <= now,
                            ),
                        ),
                    )
                )
                .order_by(
                    ChallengeImageCleanupJob.available_at,
                    ChallengeImageCleanupJob.created_at,
                )
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
            return job.id, job.challenge_id, job.image_reference, job.attempt_count

    async def _finish_success(self, job_id: object, attempt_count: int) -> bool:
        async with async_session() as db:
            job = (
                await db.execute(
                    select(ChallengeImageCleanupJob)
                    .where(
                        ChallengeImageCleanupJob.id == job_id,
                        ChallengeImageCleanupJob.attempt_count == attempt_count,
                        ChallengeImageCleanupJob.status == _STATUS_PROCESSING,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                await db.rollback()
                return False
            job.status = _STATUS_COMPLETED
            job.completed_at = utc_now()
            job.lease_until = None
            job.last_error_code = None
            await db.commit()
            return True

    async def _finish_failure(self, job_id: object, attempt_count: int, code: str) -> bool:
        async with async_session() as db:
            job = (
                await db.execute(
                    select(ChallengeImageCleanupJob)
                    .where(
                        ChallengeImageCleanupJob.id == job_id,
                        ChallengeImageCleanupJob.attempt_count == attempt_count,
                        ChallengeImageCleanupJob.status == _STATUS_PROCESSING,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                await db.rollback()
                return False
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
            return True

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.request_shutdown)
            except (NotImplementedError, RuntimeError):
                continue


async def run_from_environment() -> None:
    await ChallengeImageCleanupRunner(settings_from_environment()).run()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_from_environment())
    except ValueError:
        logger.error("challenge_image_cleanup_runner.configuration_invalid")
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        raise SystemExit(0) from None
    except Exception:
        logger.error("challenge_image_cleanup_runner.stopped_unexpectedly")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
