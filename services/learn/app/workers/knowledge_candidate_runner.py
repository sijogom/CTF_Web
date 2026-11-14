"""기본 비활성 규칙 기반 지식 후보 scheduled runner.

이 프로세스는 DB 사용자 계정을 만들거나 가장하지 않는다. enabled일 때만 공용 후보
생성 서비스를 호출하고, 실행 원장에는 ``scheduled`` trigger와 고정 runner principal을
기록한다. 로그에는 원문, 플래그, 개인정보, 예외 메시지를 남기지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from sqlalchemy.ext.asyncio import AsyncSession
from app.database import engine
from app.services.knowledge_candidate_generation import (
    CandidateGenerationInvocation,
    CandidateGenerationServiceError,
    CandidateRunnerConfigurationError,
    SCHEDULED_TRIGGER_KIND,
    ScheduledCandidateRunnerSettings,
    generate_rule_based_candidate_run,
    release_scheduled_candidate_runner_lock,
    scheduled_candidate_runner_settings_from_environment,
    try_acquire_scheduled_candidate_runner_lock,
)


logger = logging.getLogger(__name__)


class KnowledgeCandidateRunner:
    """한 프로세스/여러 컨테이너 모두에서 한 번에 하나만 실행하는 runner."""

    def __init__(self, settings: ScheduledCandidateRunnerSettings) -> None:
        self._settings = settings
        self._stop_event = asyncio.Event()
        self._run_lock = asyncio.Lock()

    def request_shutdown(self) -> None:
        if not self._stop_event.is_set():
            self._stop_event.set()
            logger.info("knowledge_candidate_runner.shutdown_requested")

    async def run(self) -> None:
        self._install_signal_handlers()
        if not self._settings.enabled:
            logger.info("knowledge_candidate_runner.disabled")
        else:
            logger.info("knowledge_candidate_runner.started")
        try:
            while not self._stop_event.is_set():
                if self._settings.enabled:
                    await self.run_once()
                await self._wait_for_next_interval()
        finally:
            logger.info("knowledge_candidate_runner.stopped")

    async def run_once(self) -> str:
        """한 tick을 실행한다. 잠겨 있거나 비활성이면 DB 변경 없이 종료한다."""
        if not self._settings.enabled:
            return "disabled"
        if self._run_lock.locked():
            logger.info("knowledge_candidate_runner.skipped_in_process_lock")
            return "locked"

        async with self._run_lock:
            async with engine.connect() as connection:
                async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                    acquired = False
                    try:
                        acquired = await try_acquire_scheduled_candidate_runner_lock(db)
                    except asyncio.CancelledError:
                        await asyncio.shield(connection.invalidate())
                        raise
                    except Exception:
                        await connection.invalidate()
                        logger.warning("knowledge_candidate_runner.lock_unavailable")
                        return "retry"
    
                    if not acquired:
                        await db.rollback()
                        logger.info("knowledge_candidate_runner.skipped_database_lock")
                        return "locked"
    
                    try:
                        result = await generate_rule_based_candidate_run(
                            db,
                            invocation=CandidateGenerationInvocation(
                                dry_run=False,
                                max_candidates=self._settings.max_candidates,
                                trigger_kind=SCHEDULED_TRIGGER_KIND,
                                execution_principal=self._settings.execution_principal,
                                requested_by=None,
                            ),
                        )
                        if result.reused_existing_run:
                            logger.info("knowledge_candidate_runner.reused_existing_run")
                            return "reused"
                        logger.info(
                            "knowledge_candidate_runner.succeeded output_count=%d duplicate_count=%d error_count=%d",
                            result.run.output_count,
                            result.run.duplicate_count,
                            result.run.error_count,
                        )
                        return "succeeded"
                    except asyncio.CancelledError:
                        raise
                    except CandidateGenerationServiceError as error:
                        # 서비스는 고정 allowlist 오류 코드만 제공한다. 예외 원문은 로그에 넣지 않는다.
                        logger.warning("knowledge_candidate_runner.retryable_failure code=%s", error.code)
                        return "retry"
                    except Exception:
                        logger.error("knowledge_candidate_runner.unexpected_failure")
                        return "retry"
                    finally:
                        try:
                            await release_scheduled_candidate_runner_lock(db)
                        except asyncio.CancelledError:
                            await asyncio.shield(connection.invalidate())
                            raise
                        except Exception:
                            await connection.invalidate()
                            logger.warning("knowledge_candidate_runner.lock_release_failed")

    async def _wait_for_next_interval(self) -> None:
        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=self._settings.interval_seconds,
            )
        except TimeoutError:
            return

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.request_shutdown)
            except (NotImplementedError, RuntimeError):
                # Windows는 asyncio signal handler를 지원하지 않을 수 있다.
                continue


async def run_from_environment() -> None:
    settings = scheduled_candidate_runner_settings_from_environment()
    runner = KnowledgeCandidateRunner(settings)
    await runner.run()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_from_environment())
    except CandidateRunnerConfigurationError:
        logger.error("knowledge_candidate_runner.configuration_invalid")
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        raise SystemExit(0) from None
    except Exception:
        # 실행 환경/DB 예외에는 불필요한 주소나 내부 문맥이 포함될 수 있으므로 원문을 남기지 않는다.
        logger.error("knowledge_candidate_runner.stopped_unexpectedly")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
