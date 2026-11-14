"""장터 결제 outbox를 유한 batch로 처리하고 만료 lease를 인계하는 worker."""

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.config import settings
from app.database import async_session, engine
from app.utils.auth_client import close_client, get_client, init_client
from app.utils.marketplace_settlement import TransferGateway, claim_settlements, reconcile_operation
from app.workers.inquiry_delivery_runner import LocalHealthServer

logger = logging.getLogger(__name__)


def bounded_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name}은 정수여야 합니다.") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 값이 허용 범위를 벗어났습니다.")
    return value


@dataclass
class SettlementHealth:
    poll_seconds: int
    last_success: float | None = None
    shutdown_requested: bool = False

    def is_ready(self) -> bool:
        return not self.shutdown_requested and self.last_success is not None and time.monotonic() - self.last_success < max(60, self.poll_seconds * 3)

    def health_body(self) -> bytes:
        return b'{"status":"ok","service":"marketplace-settlement-runner"}'

    def readiness_body(self) -> bytes:
        return b'{"status":"ready"}' if self.is_ready() else b'{"status":"not_ready"}'

    def metrics_body(self) -> bytes:
        return f"marketplace_settlement_ready {int(self.is_ready())}\n".encode("ascii")


async def run_once(session_factory, gateway: TransferGateway, *, batch_size: int, lease_seconds: int) -> int:
    async with session_factory() as db:
        claims = await claim_settlements(db, batch_size=batch_size, lease_seconds=lease_seconds)
    semaphore = asyncio.Semaphore(2)
    async def reconcile_claim(claim):
        async with semaphore:
            try:
                async with session_factory() as db:
                    await reconcile_operation(db, claim.operation_id, gateway, lease_token=claim.lease_token)
            except Exception as error:
                # DB 장애와 취소 직전 crash는 lease 만료 후 복구한다. 원격 결과를 추측하지 않는다.
                logger.warning("장터 정산을 보류했습니다: %s", type(error).__name__)
    await asyncio.gather(*(reconcile_claim(claim) for claim in claims))
    return len(claims)


async def run_from_environment() -> None:
    if not settings.internal_api_key:
        raise ValueError("장터 정산 worker에는 INTERNAL_API_KEY가 필요합니다.")
    parsed = urlsplit(settings.auth_service_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("AUTH_SERVICE_URL 형식이 올바르지 않습니다.")
    poll_seconds = bounded_setting("MARKETPLACE_SETTLEMENT_POLL_SECONDS", 5, 1, 300)
    batch_size = bounded_setting("MARKETPLACE_SETTLEMENT_BATCH_SIZE", 10, 1, 20)
    lease_seconds = bounded_setting("MARKETPLACE_SETTLEMENT_LEASE_SECONDS", 60, 30, 600)
    health_port = bounded_setting("MARKETPLACE_SETTLEMENT_HEALTH_PORT", 8320, 1, 65535)
    health = SettlementHealth(poll_seconds)
    server = LocalHealthServer(health, health_port)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stopped.set)
        except NotImplementedError:
            signal.signal(signum, lambda *_args: loop.call_soon_threadsafe(stopped.set))
    await init_client()
    try:
        await server.start()
        gateway = TransferGateway(get_client(), settings.auth_service_url)
        while not stopped.is_set():
            try:
                await run_once(async_session, gateway, batch_size=batch_size, lease_seconds=lease_seconds)
                health.last_success = time.monotonic()
            except Exception as error:
                logger.warning("장터 정산 claim을 보류했습니다: %s", type(error).__name__)
            try:
                await asyncio.wait_for(stopped.wait(), timeout=poll_seconds)
            except TimeoutError:
                pass
    finally:
        health.shutdown_requested = True
        await server.close()
        await close_client()
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_from_environment())


if __name__ == "__main__":
    main()
