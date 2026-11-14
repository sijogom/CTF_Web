"""레슨 본문 이미지의 참조 재조정과 고아 객체 정리 worker."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, false, or_, select, true

from app.database import async_session
from app.models.course import Lesson
from app.models.lesson_image import LessonImageAsset
from app.utils.education_visibility import utc_now
from app.utils.minio_client import delete_lesson_image


logger = logging.getLogger(__name__)

_STATE_STAGED = "staged"
_STATE_ATTACHED = "attached"
_STATE_ORPHAN = "orphan_candidate"
_STATE_PENDING = "cleanup_pending"
_STATE_DELETED = "deleted"
_ACTIVE_STATES = frozenset(
    {_STATE_STAGED, _STATE_ATTACHED, _STATE_ORPHAN, _STATE_PENDING}
)
_REFERENCE_SCOPE_BLOCK_CODE = "reference_scope_unknown"
_IMAGE_REFERENCE_RE = re.compile(
    r"/lesson-images/([a-f0-9]{12})/([a-f0-9]{8}\.(?:png|jpg|jpeg|gif|webp))(?:[?#\s\"')]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReconcileSettings:
    enabled: bool = True
    poll_seconds: float = 60.0
    staged_grace_seconds: int = 24 * 60 * 60
    orphan_grace_seconds: int = 24 * 60 * 60
    lease_seconds: int = 120
    batch_size: int = 100


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


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


def settings_from_environment() -> ReconcileSettings:
    return ReconcileSettings(
        enabled=_env_bool("LESSON_IMAGE_RECONCILE_ENABLED", True),
        poll_seconds=_env_float("LESSON_IMAGE_RECONCILE_POLL_SECONDS", 60.0, minimum=1.0),
        staged_grace_seconds=_env_int(
            "LESSON_IMAGE_STAGED_GRACE_SECONDS", 24 * 60 * 60, minimum=60
        ),
        orphan_grace_seconds=_env_int(
            "LESSON_IMAGE_ORPHAN_GRACE_SECONDS", 24 * 60 * 60, minimum=60
        ),
        lease_seconds=_env_int("LESSON_IMAGE_RECONCILE_LEASE_SECONDS", 120, minimum=10),
        batch_size=_env_int("LESSON_IMAGE_RECONCILE_BATCH_SIZE", 100, minimum=1),
    )


def _referenced_object_paths(contents: list[str | None]) -> set[str]:
    referenced: set[str] = set()
    for content in contents:
        if not content:
            continue
        for folder_id, filename in _IMAGE_REFERENCE_RE.findall(content):
            referenced.add(f"lesson-assets/images/{folder_id.lower()}/{filename.lower()}")
    return referenced


def _safe_error_code(error: BaseException) -> str:
    if isinstance(error, ValueError):
        return "invalid_object_path"
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return "storage_unavailable"
    return "unexpected_storage_error"


def _reconcile_candidate_filter(
    referenced: set[str],
    *,
    now,
    staged_cutoff,
):
    """Return only rows that can make a state transition in this pass.

    Keeping no-op attached and fresh staged rows out of the limited query lets
    later rows progress without weakening either grace period. Unknown states
    are deliberately absent from every branch and therefore fail closed.
    """

    if referenced:
        paths = sorted(referenced)
        is_referenced = LessonImageAsset.object_path.in_(paths)
        is_unreferenced = LessonImageAsset.object_path.notin_(paths)
        referenced_transition = and_(
            is_referenced,
            LessonImageAsset.state.in_(_ACTIVE_STATES),
            LessonImageAsset.state != _STATE_ATTACHED,
        )
    else:
        is_unreferenced = true()
        referenced_transition = false()

    return or_(
        referenced_transition,
        and_(is_unreferenced, LessonImageAsset.state == _STATE_ATTACHED),
        and_(
            is_unreferenced,
            LessonImageAsset.state == _STATE_STAGED,
            LessonImageAsset.created_at <= staged_cutoff,
        ),
        and_(
            is_unreferenced,
            LessonImageAsset.state == _STATE_ORPHAN,
            LessonImageAsset.cleanup_after.is_not(None),
            LessonImageAsset.cleanup_after <= now,
            or_(
                LessonImageAsset.last_error_code.is_(None),
                LessonImageAsset.last_error_code != _REFERENCE_SCOPE_BLOCK_CODE,
            ),
        ),
        and_(
            is_unreferenced,
            LessonImageAsset.state == _STATE_PENDING,
            or_(
                LessonImageAsset.lease_until.is_(None),
                LessonImageAsset.lease_until <= now,
            ),
            or_(
                LessonImageAsset.last_error_code.is_(None),
                LessonImageAsset.last_error_code != _REFERENCE_SCOPE_BLOCK_CODE,
            ),
        ),
    )


def _reconcile_asset_state(
    asset: LessonImageAsset,
    referenced: set[str],
    *,
    now,
    staged_cutoff,
    orphan_grace_seconds: int,
) -> bool:
    """Apply one allowlisted state transition and report whether deletion would be due."""

    # Corrupt or future states must never fall through to object deletion.
    if asset.state not in _ACTIVE_STATES:
        return False

    if asset.object_path in referenced:
        asset.state = _STATE_ATTACHED
        asset.last_seen_at = now
        asset.cleanup_after = None
        asset.lease_until = None
        asset.last_error_code = None
        return False

    if asset.state == _STATE_ATTACHED:
        asset.state = _STATE_ORPHAN
        asset.cleanup_after = now + timedelta(seconds=orphan_grace_seconds)
        asset.lease_until = None
        return False

    if asset.state == _STATE_STAGED:
        if asset.created_at > staged_cutoff:
            return False
        asset.state = _STATE_ORPHAN
        asset.cleanup_after = now + timedelta(seconds=orphan_grace_seconds)
        asset.lease_until = None
        return False

    if asset.state == _STATE_ORPHAN:
        if asset.cleanup_after is None or asset.cleanup_after > now:
            return False
    elif asset.state == _STATE_PENDING:
        if asset.lease_until is not None and asset.lease_until > now:
            return False
    else:
        # Every allowlisted state has an explicit branch above. Keep this
        # defensive return so a later state addition cannot become deletable.
        return False

    # Reference coverage is not yet complete across services and Auth-owned
    # assets. The caller records the blocked reason without claiming a delete
    # lease or changing this row's current lifecycle state.
    return True


class LessonImageReconcileRunner:
    def __init__(self, settings: ReconcileSettings) -> None:
        self._settings = settings
        self._stop_event = asyncio.Event()

    def request_shutdown(self) -> None:
        if not self._stop_event.is_set():
            self._stop_event.set()
            logger.info("lesson_image_reconcile_runner.shutdown_requested")

    async def run(self) -> None:
        self._install_signal_handlers()
        if not self._settings.enabled:
            logger.info("lesson_image_reconcile_runner.disabled")
            return
        logger.info("lesson_image_reconcile_runner.started")
        previous_status: str | None = None
        try:
            while not self._stop_event.is_set():
                current_status = await self.run_once()
                if current_status != previous_status:
                    if current_status == "blocked_reference_scope":
                        logger.warning(
                            "lesson_image_reconcile_runner.cleanup_blocked."
                            "reference_scope_unknown"
                        )
                    elif previous_status == "blocked_reference_scope":
                        logger.info(
                            "lesson_image_reconcile_runner.cleanup_block_cleared"
                        )
                previous_status = current_status
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._settings.poll_seconds
                    )
                except TimeoutError:
                    continue
        finally:
            logger.info("lesson_image_reconcile_runner.stopped")

    async def run_once(self) -> str:
        contents = await self._load_lesson_contents()
        referenced = _referenced_object_paths(contents)
        due, reference_scope_blocked = await self._reconcile_rows(referenced)
        if reference_scope_blocked:
            return "blocked_reference_scope"
        if not due:
            return "idle"
        completed = 0
        for asset_id, object_path in due:
            try:
                await asyncio.to_thread(delete_lesson_image, object_path)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._mark_failure(asset_id, _safe_error_code(error))
                continue
            await self._mark_deleted(asset_id)
            completed += 1
        return "completed" if completed == len(due) else "partial_failure"

    async def _load_lesson_contents(self) -> list[str | None]:
        async with async_session() as db:
            result = await db.execute(select(Lesson.content))
            return list(result.scalars().all())

    async def _reconcile_rows(
        self,
        referenced: set[str],
    ) -> tuple[list[tuple[object, str]], bool]:
        now = utc_now()
        staged_cutoff = now - timedelta(seconds=self._settings.staged_grace_seconds)
        due: list[tuple[object, str]] = []
        async with async_session() as db:
            result = await db.execute(
                select(LessonImageAsset)
                .where(
                    _reconcile_candidate_filter(
                        referenced,
                        now=now,
                        staged_cutoff=staged_cutoff,
                    )
                )
                .order_by(LessonImageAsset.created_at, LessonImageAsset.id)
                .with_for_update(skip_locked=True)
                .limit(self._settings.batch_size)
            )
            for asset in result.scalars().all():
                if _reconcile_asset_state(
                    asset,
                    referenced,
                    now=now,
                    staged_cutoff=staged_cutoff,
                    orphan_grace_seconds=self._settings.orphan_grace_seconds,
                ):
                    # The current registry cannot prove all cross-service and
                    # Auth-owned references. Preserve the lifecycle state and
                    # record why this otherwise-due object was not deleted.
                    asset.last_error_code = _REFERENCE_SCOPE_BLOCK_CODE
            blocked_result = await db.execute(
                select(LessonImageAsset.id)
                .where(
                    LessonImageAsset.last_error_code
                    == _REFERENCE_SCOPE_BLOCK_CODE
                )
                .limit(1)
            )
            reference_scope_blocked = blocked_result.scalar_one_or_none() is not None
            await db.commit()
        return due, reference_scope_blocked

    async def _mark_deleted(self, asset_id: object) -> None:
        async with async_session() as db:
            asset = (
                await db.execute(
                    select(LessonImageAsset)
                    .where(LessonImageAsset.id == asset_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if asset is None:
                await db.rollback()
                return
            asset.state = _STATE_DELETED
            asset.deleted_at = utc_now()
            asset.lease_until = None
            asset.cleanup_after = None
            asset.last_error_code = None
            await db.commit()

    async def _mark_failure(self, asset_id: object, code: str) -> None:
        async with async_session() as db:
            asset = (
                await db.execute(
                    select(LessonImageAsset)
                    .where(LessonImageAsset.id == asset_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if asset is None:
                await db.rollback()
                return
            asset.state = _STATE_ORPHAN
            asset.lease_until = None
            asset.last_error_code = code
            delay = min(2 ** max(asset.attempt_count - 1, 0), 15 * 60)
            asset.cleanup_after = utc_now() + timedelta(seconds=delay)
            await db.commit()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.request_shutdown)
            except (NotImplementedError, RuntimeError):
                continue


async def run_from_environment() -> None:
    await LessonImageReconcileRunner(settings_from_environment()).run()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_from_environment())
    except ValueError:
        logger.error("lesson_image_reconcile_runner.configuration_invalid")
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        raise SystemExit(0) from None
    except Exception:
        logger.error("lesson_image_reconcile_runner.stopped_unexpectedly")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
