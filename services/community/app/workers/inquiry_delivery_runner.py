"""Trusted runner for Community inquiry-notification outbox delivery.

The public Community API never calls SMTP.  This process is the only component
that bridges the Community outbox and Auth's internal mail-delivery endpoint:

1. lease work through Community's internal claim endpoint;
2. send the minimal notification payload to Auth;
3. complete the same lease as ``sent``, ``skipped``, or ``retry``.

Raw recipient addresses, signed access paths, lease tokens, request bodies, and
internal keys are deliberately kept out of logs and health/metrics output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import UUID

import httpx


logger = logging.getLogger(__name__)

_TERMINAL_STATES = frozenset({"sent", "skipped"})
_COMPLETE_STATES = frozenset({"sent", "skipped", "retry"})
_RECIPIENT_TYPES = frozenset({"guest", "member"})
_NOTIFICATION_KINDS = frozenset({"guest_access", "reply_available"})
_SUPPORTED_LOCALES = frozenset({"ko", "en", "ja", "zh"})


class RunnerConfigurationError(ValueError):
    """Raised before startup when a non-secret runner setting is invalid."""


class ClaimContractError(ValueError):
    """Raised when an internal Community claim does not match the fixed contract."""


@dataclass(frozen=True)
class RunnerSettings:
    """Environment-only runner configuration with conservative bounds."""

    community_service_url: str
    auth_service_url: str
    internal_api_key: str
    worker_id: str
    poll_seconds: float
    batch_size: int
    http_timeout_seconds: float
    health_port: int

    @property
    def ready_after_seconds(self) -> float:
        """A failed dependency becomes visible without making idle workers unready."""
        return max(30.0, self.poll_seconds * 3)

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "RunnerSettings":
        values = os.environ if environ is None else environ
        internal_api_key = values.get("INTERNAL_API_KEY", "").strip()
        if not internal_api_key:
            raise RunnerConfigurationError("INTERNAL_API_KEY is required")

        worker_id = values.get("INQUIRY_RUNNER_ID", "").strip()
        if not worker_id:
            worker_id = f"inquiry-{socket.gethostname()}-{os.getpid()}"
        if (
            len(worker_id) > 100
            or not worker_id
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for character in worker_id)
        ):
            raise RunnerConfigurationError("INQUIRY_RUNNER_ID is invalid")

        return cls(
            community_service_url=_normalize_internal_service_url(
                values.get("COMMUNITY_SERVICE_URL", "http://community-service:8300"),
                setting_name="COMMUNITY_SERVICE_URL",
            ),
            auth_service_url=_normalize_internal_service_url(
                values.get("AUTH_SERVICE_URL", "http://auth-service:8020"),
                setting_name="AUTH_SERVICE_URL",
            ),
            internal_api_key=internal_api_key,
            worker_id=worker_id,
            poll_seconds=_read_bounded_float(
                values,
                "INQUIRY_RUNNER_POLL_SECONDS",
                default=2.0,
                minimum=0.1,
                maximum=300.0,
            ),
            batch_size=_read_bounded_int(
                values,
                "INQUIRY_RUNNER_BATCH_SIZE",
                default=5,
                minimum=1,
                maximum=10,
            ),
            http_timeout_seconds=_read_bounded_float(
                values,
                "INQUIRY_RUNNER_HTTP_TIMEOUT_SECONDS",
                default=8.0,
                minimum=1.0,
                maximum=8.0,
            ),
            health_port=_read_bounded_int(
                values,
                "INQUIRY_RUNNER_HEALTH_PORT",
                default=8310,
                minimum=1,
                maximum=65535,
            ),
        )


def _normalize_internal_service_url(value: str, *, setting_name: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RunnerConfigurationError(f"{setting_name} is invalid")
    return candidate


def _read_bounded_float(
    values: Mapping[str, str],
    setting_name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw_value = values.get(setting_name, str(default)).strip()
    try:
        value = float(raw_value)
    except ValueError as error:
        raise RunnerConfigurationError(f"{setting_name} must be numeric") from error
    if not minimum <= value <= maximum:
        raise RunnerConfigurationError(f"{setting_name} is outside its allowed range")
    return value


def _read_bounded_int(
    values: Mapping[str, str],
    setting_name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = values.get(setting_name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RunnerConfigurationError(f"{setting_name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise RunnerConfigurationError(f"{setting_name} is outside its allowed range")
    return value


@dataclass(frozen=True)
class NotificationClaim:
    """Validated minimum payload returned by the Community claim endpoint."""

    event_id: str
    lease_token: str
    recipient_type: str
    recipient_email: str | None
    recipient_user_id: str | None
    kind: str
    access_path: str
    locale: str

    @classmethod
    def from_wire(cls, value: object) -> "NotificationClaim":
        if not isinstance(value, Mapping):
            raise ClaimContractError("claim must be an object")

        event_id = _require_uuid_text(value.get("event_id"), "event_id")
        lease_token = _require_text(value.get("lease_token"), "lease_token", minimum=32, maximum=255)
        recipient_type = _require_text(value.get("recipient_type"), "recipient_type", minimum=1, maximum=20)
        kind = _require_text(value.get("kind"), "kind", minimum=1, maximum=30)
        access_path = _require_text(value.get("access_path"), "access_path", minimum=1, maximum=2048)
        locale = _require_text(value.get("locale"), "locale", minimum=2, maximum=10).lower()

        if recipient_type not in _RECIPIENT_TYPES:
            raise ClaimContractError("recipient_type is invalid")
        if kind not in _NOTIFICATION_KINDS:
            raise ClaimContractError("kind is invalid")
        if not access_path.startswith("/inquiry"):
            raise ClaimContractError("access_path is invalid")
        if locale not in _SUPPORTED_LOCALES:
            raise ClaimContractError("locale is invalid")

        recipient_email = value.get("recipient_email")
        recipient_user_id = value.get("recipient_user_id")
        if recipient_type == "guest":
            if not isinstance(recipient_email, str) or not recipient_email or recipient_user_id is not None:
                raise ClaimContractError("guest recipient is invalid")
            return cls(
                event_id=event_id,
                lease_token=lease_token,
                recipient_type=recipient_type,
                recipient_email=recipient_email,
                recipient_user_id=None,
                kind=kind,
                access_path=access_path,
                locale=locale,
            )

        if recipient_email is not None:
            raise ClaimContractError("member recipient email is invalid")
        return cls(
            event_id=event_id,
            lease_token=lease_token,
            recipient_type=recipient_type,
            recipient_email=None,
            recipient_user_id=_require_uuid_text(recipient_user_id, "recipient_user_id"),
            kind=kind,
            access_path=access_path,
            locale=locale,
        )

    def auth_payload(self) -> dict[str, str | None]:
        """Return only fields Auth's fixed internal contract accepts."""
        return {
            "event_id": self.event_id,
            "recipient_type": self.recipient_type,
            "recipient_email": self.recipient_email,
            "recipient_user_id": self.recipient_user_id,
            "kind": self.kind,
            "access_path": self.access_path,
            "locale": self.locale,
        }


def _require_text(value: object, name: str, *, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ClaimContractError(f"{name} is invalid")
    return value


def _require_uuid_text(value: object, name: str) -> str:
    text = _require_text(value, name, minimum=36, maximum=36)
    try:
        return str(UUID(text))
    except (ValueError, AttributeError) as error:
        raise ClaimContractError(f"{name} is invalid") from error


@dataclass
class RunnerMetrics:
    """Non-sensitive monotonic counters exported only from the local health port."""

    claims_total: int = 0
    sent_total: int = 0
    skipped_total: int = 0
    retry_total: int = 0
    claim_failures_total: int = 0
    auth_failures_total: int = 0
    completion_failures_total: int = 0
    invalid_claims_total: int = 0


class RunnerHealth:
    """Tracks process health separately from Community/Auth readiness."""

    def __init__(self, settings: RunnerSettings, metrics: RunnerMetrics) -> None:
        self._settings = settings
        self._metrics = metrics
        self.started_at = time.monotonic()
        self.last_successful_claim_poll: float | None = None
        self.shutdown_requested = False

    def mark_claim_poll_success(self) -> None:
        self.last_successful_claim_poll = time.monotonic()

    def is_ready(self) -> bool:
        if self.shutdown_requested or self.last_successful_claim_poll is None:
            return False
        return time.monotonic() - self.last_successful_claim_poll <= self._settings.ready_after_seconds

    def health_body(self) -> bytes:
        return json.dumps(
            {
                "status": "ok",
                "service": "community-inquiry-runner",
                "shutdown_requested": self.shutdown_requested,
            },
            separators=(",", ":"),
        ).encode("utf-8")

    def readiness_body(self) -> bytes:
        return json.dumps(
            {
                "status": "ready" if self.is_ready() else "not_ready",
                "service": "community-inquiry-runner",
            },
            separators=(",", ":"),
        ).encode("utf-8")

    def metrics_body(self) -> bytes:
        metrics = self._metrics
        return (
            "# TYPE inquiry_delivery_claims_total counter\n"
            f"inquiry_delivery_claims_total {metrics.claims_total}\n"
            "# TYPE inquiry_delivery_sent_total counter\n"
            f"inquiry_delivery_sent_total {metrics.sent_total}\n"
            "# TYPE inquiry_delivery_skipped_total counter\n"
            f"inquiry_delivery_skipped_total {metrics.skipped_total}\n"
            "# TYPE inquiry_delivery_retry_total counter\n"
            f"inquiry_delivery_retry_total {metrics.retry_total}\n"
            "# TYPE inquiry_delivery_claim_failures_total counter\n"
            f"inquiry_delivery_claim_failures_total {metrics.claim_failures_total}\n"
            "# TYPE inquiry_delivery_auth_failures_total counter\n"
            f"inquiry_delivery_auth_failures_total {metrics.auth_failures_total}\n"
            "# TYPE inquiry_delivery_completion_failures_total counter\n"
            f"inquiry_delivery_completion_failures_total {metrics.completion_failures_total}\n"
            "# TYPE inquiry_delivery_invalid_claims_total counter\n"
            f"inquiry_delivery_invalid_claims_total {metrics.invalid_claims_total}\n"
            "# TYPE inquiry_delivery_ready gauge\n"
            f"inquiry_delivery_ready {1 if self.is_ready() else 0}\n"
        ).encode("utf-8")


class LocalHealthServer:
    """Minimal loopback-only HTTP server for Docker health and metrics checks."""

    def __init__(self, health: RunnerHealth, port: int) -> None:
        self._health = health
        self._port = port
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection,
            host="127.0.0.1",
            port=self._port,
        )

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        status = 400
        content_type = "application/json; charset=utf-8"
        body = b'{"status":"bad_request"}'
        try:
            first_line = await asyncio.wait_for(reader.readline(), timeout=1.0)
            parts = first_line.decode("ascii", "replace").strip().split(" ")
            if len(parts) == 3 and parts[0] == "GET":
                await self._discard_headers(reader)
                path = parts[1].split("?", 1)[0]
                if path == "/health":
                    status = 200
                    body = self._health.health_body()
                elif path == "/ready":
                    status = 200 if self._health.is_ready() else 503
                    body = self._health.readiness_body()
                elif path == "/metrics":
                    status = 200
                    content_type = "text/plain; version=0.0.4; charset=utf-8"
                    body = self._health.metrics_body()
                else:
                    status = 404
                    body = b'{"status":"not_found"}'
        except (OSError, TimeoutError, UnicodeError):
            status = 400
            body = b'{"status":"bad_request"}'
        finally:
            reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 503: "Service Unavailable"}[status]
            response = (
                f"HTTP/1.1 {status} {reason}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii") + body
            try:
                writer.write(response)
                await writer.drain()
            except OSError:
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    @staticmethod
    async def _discard_headers(reader: asyncio.StreamReader) -> None:
        for _ in range(32):
            header = await asyncio.wait_for(reader.readline(), timeout=1.0)
            if header in {b"", b"\r\n", b"\n"}:
                return
            if len(header) > 8192:
                return


class InquiryDeliveryRunner:
    """Poll and deliver one bounded Community outbox batch at a time."""

    def __init__(self, settings: RunnerSettings, http_client: Any) -> None:
        self._settings = settings
        self._http_client = http_client
        self.metrics = RunnerMetrics()
        self.health = RunnerHealth(settings, self.metrics)
        self._stop_event = asyncio.Event()
        self._health_server = LocalHealthServer(self.health, settings.health_port)

    def request_shutdown(self) -> None:
        if not self._stop_event.is_set():
            self.health.shutdown_requested = True
            logger.info("inquiry_delivery_runner.shutdown_requested")
            self._stop_event.set()

    async def run(self) -> None:
        await self._health_server.start()
        self._install_signal_handlers()
        logger.info("inquiry_delivery_runner.started")
        try:
            while not self._stop_event.is_set():
                await self.run_once()
                await self._wait_for_next_poll()
        finally:
            await self._health_server.close()
            logger.info("inquiry_delivery_runner.stopped")

    async def run_once(self) -> int:
        """Claim a batch and attempt every claimed command unless shutdown begins."""
        if self._stop_event.is_set():
            return 0
        claims = await self._claim_batch()
        for claim in claims:
            if self._stop_event.is_set():
                break
            await self._deliver_claim(claim)
        return len(claims)

    async def _claim_batch(self) -> list[NotificationClaim]:
        try:
            response = await self._http_client.post(
                f"{self._settings.community_service_url}/inquiries/internal/outbox/claim",
                json={
                    "worker_id": self._settings.worker_id,
                    "limit": self._settings.batch_size,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.metrics.claim_failures_total += 1
            logger.warning("inquiry_delivery_runner.claim_failed")
            return []

        if response.status_code != 200:
            self.metrics.claim_failures_total += 1
            logger.warning("inquiry_delivery_runner.claim_rejected status=%d", response.status_code)
            return []

        try:
            payload = _response_object(response)
            raw_items = payload.get("items")
            if not isinstance(raw_items, list) or len(raw_items) > self._settings.batch_size:
                raise ClaimContractError("claim response is invalid")
            claims = [NotificationClaim.from_wire(item) for item in raw_items]
        except ClaimContractError:
            self.metrics.invalid_claims_total += 1
            logger.error("inquiry_delivery_runner.invalid_claim_response")
            return []

        self.metrics.claims_total += len(claims)
        self.health.mark_claim_poll_success()
        return claims

    async def _deliver_claim(self, claim: NotificationClaim) -> None:
        result, error_code = await self._send_to_auth(claim)
        await self._complete_claim(claim, result=result, error_code=error_code)

    async def _send_to_auth(self, claim: NotificationClaim) -> tuple[str, str | None]:
        try:
            response = await self._http_client.post(
                f"{self._settings.auth_service_url}/internal/inquiry-notifications",
                json=claim.auth_payload(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.metrics.auth_failures_total += 1
            logger.warning("inquiry_delivery_runner.auth_unavailable")
            return "retry", "auth_unavailable"

        if response.status_code == 200:
            try:
                status_value = _response_object(response).get("status")
            except ClaimContractError:
                status_value = None
            if status_value in _TERMINAL_STATES:
                return str(status_value), None
            self.metrics.auth_failures_total += 1
            logger.error("inquiry_delivery_runner.auth_invalid_response")
            return "retry", "auth_invalid_response"

        self.metrics.auth_failures_total += 1
        if response.status_code == 409:
            logger.warning("inquiry_delivery_runner.auth_in_progress")
            return "retry", "auth_in_progress"
        if 400 <= response.status_code < 500:
            logger.error("inquiry_delivery_runner.auth_rejected status=%d", response.status_code)
            return "retry", "auth_rejected"
        logger.warning("inquiry_delivery_runner.auth_unavailable status=%d", response.status_code)
        return "retry", "auth_unavailable"

    async def _complete_claim(
        self,
        claim: NotificationClaim,
        *,
        result: str,
        error_code: str | None,
    ) -> bool:
        if result not in _COMPLETE_STATES:
            raise ValueError("unsupported completion state")
        payload: dict[str, str] = {"lease_token": claim.lease_token, "result": result}
        if error_code is not None:
            payload["error"] = error_code

        for attempt in range(2):
            try:
                response = await self._http_client.post(
                    f"{self._settings.community_service_url}/inquiries/internal/outbox/{claim.event_id}/complete",
                    json=payload,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                response = None

            if response is not None and response.status_code == 200:
                try:
                    state = _response_object(response).get("state")
                except ClaimContractError:
                    state = None
                if state in _COMPLETE_STATES:
                    self._record_completion(str(state))
                    return True

            if attempt == 0:
                await self._short_completion_retry_delay()

        self.metrics.completion_failures_total += 1
        logger.error("inquiry_delivery_runner.complete_failed")
        return False

    def _record_completion(self, state: str) -> None:
        if state == "sent":
            self.metrics.sent_total += 1
        elif state == "skipped":
            self.metrics.skipped_total += 1
        else:
            self.metrics.retry_total += 1

    async def _wait_for_next_poll(self) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=self._settings.poll_seconds)
        except TimeoutError:
            return

    async def _short_completion_retry_delay(self) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=0.25)
        except TimeoutError:
            return

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.request_shutdown)
            except (NotImplementedError, RuntimeError):
                # Windows does not support asyncio signal handlers. The runner's
                # production target is Linux, and Ctrl+C still terminates locally.
                continue


def _response_object(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise ClaimContractError("internal response is not JSON") from error
    if not isinstance(payload, dict):
        raise ClaimContractError("internal response is not an object")
    return payload


def build_http_client(settings: RunnerSettings) -> httpx.AsyncClient:
    """Create the only outbound client with no proxy inheritance or redirects."""
    return httpx.AsyncClient(
        headers={"X-Internal-Key": settings.internal_api_key},
        timeout=httpx.Timeout(settings.http_timeout_seconds),
        follow_redirects=False,
        trust_env=False,
    )


async def run_from_environment() -> None:
    settings = RunnerSettings.from_environment()
    async with build_http_client(settings) as http_client:
        runner = InquiryDeliveryRunner(settings, http_client)
        await runner.run()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_from_environment())
    except RunnerConfigurationError:
        logger.error("inquiry_delivery_runner.configuration_invalid")
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        raise SystemExit(0) from None
    except Exception:
        # Never include exception text: it can contain an upstream URL or other
        # runtime context that is unnecessary for the operator-facing log.
        logger.error("inquiry_delivery_runner.stopped_unexpectedly")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
