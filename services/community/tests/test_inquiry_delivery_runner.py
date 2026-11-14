"""Database-free contract tests for the trusted inquiry delivery runner."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import unittest
from pathlib import Path

import httpx


def _load_runner_module():
    source_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "workers"
        / "inquiry_delivery_runner.py"
    )
    spec = importlib.util.spec_from_file_location("inquiry_delivery_runner_test_module", source_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeHttpClient:
    def __init__(self, responses: list[FakeResponse | BaseException]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def post(self, url: str, *, json: dict[str, object]) -> FakeResponse:
        self.calls.append((url, json))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class InquiryDeliveryRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._previous_logging_disable = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        cls.runner_module = _load_runner_module()

    @classmethod
    def tearDownClass(cls) -> None:
        logging.disable(cls._previous_logging_disable)

    def _settings(self):
        return self.runner_module.RunnerSettings(
            community_service_url="http://community-service:8300",
            auth_service_url="http://auth-service:8020",
            internal_api_key="x" * 32,
            worker_id="test-runner",
            poll_seconds=2.0,
            batch_size=5,
            http_timeout_seconds=8.0,
            health_port=8310,
        )

    @staticmethod
    def _guest_claim() -> dict[str, str | None]:
        return {
            "event_id": "00000000-0000-4000-8000-000000000001",
            "lease_token": "a" * 32,
            "recipient_type": "guest",
            "recipient_email": "guest@example.test",
            "recipient_user_id": None,
            "kind": "guest_access",
            "access_path": "/inquiry/access#ticket=1&challenge=2&proof=3",
            "locale": "ko",
        }

    def test_settings_rejects_empty_key_and_credentialed_service_url(self) -> None:
        with self.assertRaises(self.runner_module.RunnerConfigurationError):
            self.runner_module.RunnerSettings.from_environment({})

        with self.assertRaises(self.runner_module.RunnerConfigurationError):
            self.runner_module.RunnerSettings.from_environment(
                {
                    "INTERNAL_API_KEY": "x" * 32,
                    "COMMUNITY_SERVICE_URL": "http://user:pass@community-service:8300",
                }
            )

    def test_settings_bound_timeout_to_the_compose_shutdown_window(self) -> None:
        accepted = self.runner_module.RunnerSettings.from_environment(
            {
                "INTERNAL_API_KEY": "x" * 32,
                "INQUIRY_RUNNER_HTTP_TIMEOUT_SECONDS": "8",
            }
        )
        self.assertEqual(accepted.http_timeout_seconds, 8.0)

        with self.assertRaises(self.runner_module.RunnerConfigurationError):
            self.runner_module.RunnerSettings.from_environment(
                {
                    "INTERNAL_API_KEY": "x" * 32,
                    "INQUIRY_RUNNER_HTTP_TIMEOUT_SECONDS": "8.1",
                }
            )

    def test_sent_result_calls_auth_without_lease_and_completes_same_event(self) -> None:
        client = FakeHttpClient(
            [
                FakeResponse(200, {"items": [self._guest_claim()]}),
                FakeResponse(200, {"status": "sent"}),
                FakeResponse(200, {"state": "sent"}),
            ]
        )
        runner = self.runner_module.InquiryDeliveryRunner(self._settings(), client)

        claimed_count = asyncio.run(runner.run_once())

        self.assertEqual(claimed_count, 1)
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(client.calls[0][0].endswith("/inquiries/internal/outbox/claim"))
        self.assertTrue(client.calls[1][0].endswith("/internal/inquiry-notifications"))
        self.assertNotIn("lease_token", client.calls[1][1])
        self.assertTrue(client.calls[2][0].endswith("/outbox/00000000-0000-4000-8000-000000000001/complete"))
        self.assertEqual(client.calls[2][1]["result"], "sent")
        self.assertEqual(runner.metrics.sent_total, 1)
        self.assertTrue(runner.health.is_ready())

    def test_auth_transport_failure_is_completed_as_sanitized_retry(self) -> None:
        client = FakeHttpClient(
            [
                FakeResponse(200, {"items": [self._guest_claim()]}),
                httpx.ConnectError("not logged by the runner"),
                FakeResponse(200, {"state": "retry"}),
            ]
        )
        runner = self.runner_module.InquiryDeliveryRunner(self._settings(), client)

        asyncio.run(runner.run_once())

        completion_payload = client.calls[-1][1]
        self.assertEqual(completion_payload["result"], "retry")
        self.assertEqual(completion_payload["error"], "auth_unavailable")
        self.assertEqual(runner.metrics.retry_total, 1)
        self.assertEqual(runner.metrics.auth_failures_total, 1)

    def test_invalid_auth_terminal_response_is_not_treated_as_delivery_success(self) -> None:
        client = FakeHttpClient(
            [
                FakeResponse(200, {"items": [self._guest_claim()]}),
                FakeResponse(200, {"status": "processing"}),
                FakeResponse(200, {"state": "retry"}),
            ]
        )
        runner = self.runner_module.InquiryDeliveryRunner(self._settings(), client)

        asyncio.run(runner.run_once())

        self.assertEqual(client.calls[-1][1]["error"], "auth_invalid_response")
        self.assertEqual(runner.metrics.sent_total, 0)
        self.assertEqual(runner.metrics.retry_total, 1)

    def test_health_readiness_turns_off_for_graceful_shutdown(self) -> None:
        runner = self.runner_module.InquiryDeliveryRunner(self._settings(), FakeHttpClient([]))
        runner.health.mark_claim_poll_success()

        self.assertTrue(runner.health.is_ready())
        runner.request_shutdown()
        self.assertFalse(runner.health.is_ready())
        self.assertIn(b'"shutdown_requested":true', runner.health.health_body())

    def test_shutdown_does_not_start_a_new_claim(self) -> None:
        client = FakeHttpClient([])
        runner = self.runner_module.InquiryDeliveryRunner(self._settings(), client)
        runner.request_shutdown()

        claimed_count = asyncio.run(runner.run_once())

        self.assertEqual(claimed_count, 0)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
