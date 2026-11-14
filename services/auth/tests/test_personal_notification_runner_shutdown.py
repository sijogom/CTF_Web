"""Network-free shutdown contracts; POSIX subprocess coverage runs on Linux."""

import asyncio
import importlib.util
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


def load_runner():
    modules = {}
    for name in ("app", "app.models", "app.utils", "app.workers", "app.email"):
        modules[name] = ModuleType(name)
        modules[name].__path__ = []
    values = {
        "app.config": {"settings": SimpleNamespace(frontend_url="http://localhost")},
        "app.database": {"async_session": Mock(), "engine": SimpleNamespace(dispose=AsyncMock())},
        "app.email.sender": {"_get_smtp_config": AsyncMock()},
        "app.models.personal_notification": {name: Mock() for name in ("BadgeObservation", "NotificationPolicy", "PersonalNotification")},
        "app.models.user": {"User": Mock()},
        "app.utils.personal_notifications": {"create_due_competition_events": AsyncMock(), "fanout_competition_events": AsyncMock()},
        "app.utils.internal_api": {"close_client": AsyncMock()},
        "app.workers.notification_health": {"NotificationWorkerHealth": Mock()},
    }
    for name, attributes in values.items():
        modules[name] = ModuleType(name)
        modules[name].__dict__.update(attributes)
    source = Path(__file__).resolve().parents[1] / "app/workers/personal_notification_runner.py"
    spec = importlib.util.spec_from_file_location("personal_shutdown_target", source)
    runner = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(runner)
    return runner


class Session:
    def __init__(self, get):
        self.get = get
        self.exits = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exception):
        self.exits.append(exception[0])


class SignalBindingTests(unittest.TestCase):
    def setUp(self):
        self.runner = load_runner()

    def test_both_signals_cancel_once_and_restore_previous_handlers(self):
        loop, task = Mock(), Mock()
        callbacks = {}
        loop.add_signal_handler.side_effect = lambda number, callback: callbacks.__setitem__(number, callback)
        previous = {number: Mock() for number in (signal.SIGTERM, signal.SIGINT)}
        with patch.object(self.runner.asyncio, "get_running_loop", return_value=loop), patch.object(self.runner.asyncio, "current_task", return_value=task), patch.object(self.runner.signal, "getsignal", side_effect=previous.get), patch.object(self.runner.signal, "signal") as restore:
            with self.runner._cancel_on_shutdown_signals():
                callbacks[signal.SIGTERM]()
                callbacks[signal.SIGINT]()
                task.cancel.assert_called_once_with()
            self.assertEqual(loop.remove_signal_handler.call_count, 2)
            self.assertEqual({call.args for call in restore.call_args_list}, set(previous.items()))

    def test_windows_fallback_schedules_cancel_thread_safely_and_restores(self):
        loop, task = Mock(), Mock()
        loop.add_signal_handler.side_effect = NotImplementedError
        loop.call_soon_threadsafe.side_effect = lambda callback: callback()
        previous = {number: Mock() for number in (signal.SIGTERM, signal.SIGINT)}
        with patch.object(self.runner.asyncio, "get_running_loop", return_value=loop), patch.object(self.runner.asyncio, "current_task", return_value=task), patch.object(self.runner.signal, "getsignal", side_effect=previous.get), patch.object(self.runner.signal, "signal") as binding:
            with self.runner._cancel_on_shutdown_signals():
                callbacks = [call.args[1] for call in binding.call_args_list]
                callbacks[0](signal.SIGTERM, None)
                callbacks[1](signal.SIGINT, None)
                task.cancel.assert_called_once_with()
                self.assertEqual(loop.call_soon_threadsafe.call_count, 2)
            self.assertEqual(binding.call_count, 4)
            loop.remove_signal_handler.assert_not_called()

    def test_second_registration_failure_rolls_back_first(self):
        loop = Mock()
        loop.add_signal_handler.side_effect = [None, RuntimeError("registration failed")]
        previous = Mock()
        with patch.object(self.runner.asyncio, "get_running_loop", return_value=loop), patch.object(self.runner.asyncio, "current_task", return_value=Mock()), patch.object(self.runner.signal, "getsignal", return_value=previous), patch.object(self.runner.signal, "signal") as restore:
            with self.assertRaisesRegex(RuntimeError, "registration failed"):
                with self.runner._cancel_on_shutdown_signals():
                    self.fail("registration failure must not enter worker body")
            loop.remove_signal_handler.assert_called_once_with(signal.SIGTERM)
            restore.assert_called_once_with(signal.SIGTERM, previous)

    def test_windows_partial_registration_failure_restores_first(self):
        loop, previous = Mock(), Mock()
        loop.add_signal_handler.side_effect = NotImplementedError
        def bind(number, handler):
            if number == signal.SIGINT and handler is not previous:
                raise RuntimeError("native registration failed")
        with patch.object(self.runner.asyncio, "get_running_loop", return_value=loop), patch.object(self.runner.asyncio, "current_task", return_value=Mock()), patch.object(self.runner.signal, "getsignal", return_value=previous), patch.object(self.runner.signal, "signal", side_effect=bind) as binding:
            with self.assertRaisesRegex(RuntimeError, "native registration failed"):
                with self.runner._cancel_on_shutdown_signals():
                    self.fail("partial native registration must not enter worker body")
            self.assertEqual(binding.call_args_list[-1].args, (signal.SIGTERM, previous))
            loop.remove_signal_handler.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "real POSIX SIGTERM subprocess requires Linux")
    def test_real_posix_sigterm_cancels_task_and_runs_finally(self):
        code = f"""
import asyncio, importlib.util
spec = importlib.util.spec_from_file_location('shutdown_tests', {str(Path(__file__).resolve())!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
runner = module.load_runner()
async def probe():
    with runner._cancel_on_shutdown_signals():
        try:
            print('READY', flush=True)
            await asyncio.Event().wait()
        finally:
            print('CLEANUP', flush=True)
try:
    asyncio.run(probe())
except asyncio.CancelledError:
    print('CANCELLED', flush=True)
"""
        process = subprocess.Popen([sys.executable, "-B", "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 10)[0], "subprocess readiness timed out")
            self.assertEqual(process.stdout.readline().strip(), "READY")
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn("CLEANUP", stdout)
            self.assertIn("CANCELLED", stdout)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


class WorkerCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runner = load_runner()
        self.loop = asyncio.get_running_loop()
        self.callbacks = {}
        self.patches = [
            patch.object(self.loop, "add_signal_handler", side_effect=lambda number, callback: self.callbacks.__setitem__(number, callback)),
            patch.object(self.loop, "remove_signal_handler"),
            patch.object(self.runner.signal, "getsignal", return_value=signal.SIG_DFL),
            patch.object(self.runner.signal, "signal"),
            patch.object(self.runner.argparse.ArgumentParser, "parse_args", return_value=SimpleNamespace(baseline_only=False)),
            patch.object(self.runner.logging, "basicConfig"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.health = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), mark_success=Mock(), mark_failure=Mock())
        self.runner.NotificationWorkerHealth.return_value = self.health
        self.session = Session(AsyncMock(return_value=SimpleNamespace(baseline_completed_at=object())))
        self.runner.async_session.return_value = self.session
        self.runner.run_event_batch = AsyncMock()
        self.runner.observe_badge_batch = AsyncMock()
        self.runner.run_email_batch = AsyncMock()

    async def cancel_during_tick(self, number=signal.SIGTERM):
        entered = asyncio.Event()
        async def blocked_tick():
            entered.set()
            await asyncio.Event().wait()
        self.runner.run_event_batch.side_effect = blocked_tick
        task = asyncio.create_task(self.runner.main())
        await asyncio.wait_for(entered.wait(), 2)
        self.callbacks[number]()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.health.close.assert_awaited_once_with()
        self.runner.close_client.assert_awaited_once_with()
        self.runner.engine.dispose.assert_awaited_once_with()
        self.runner.observe_badge_batch.assert_not_awaited()
        self.runner.run_email_batch.assert_not_awaited()

    async def test_sigterm_cancels_inflight_tick_and_closes_all_resources(self):
        await self.cancel_during_tick()

    async def test_cleanup_failure_does_not_mask_cancellation_or_skip_other_closes(self):
        self.health.close.side_effect = RuntimeError("health close failed")
        with self.assertLogs(self.runner.logger, level="ERROR"):
            await self.cancel_during_tick(signal.SIGINT)

    async def test_cancelled_policy_query_exits_db_context_and_closes_client_pool(self):
        entered = asyncio.Event()
        async def blocked_get(*args):
            entered.set()
            await asyncio.Event().wait()
        self.session.get.side_effect = blocked_get
        task = asyncio.create_task(self.runner.main())
        await asyncio.wait_for(entered.wait(), 2)
        self.callbacks[signal.SIGTERM]()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertEqual(self.session.exits, [asyncio.CancelledError])
        self.runner.NotificationWorkerHealth.assert_not_called()
        self.runner.close_client.assert_awaited_once_with()
        self.runner.engine.dispose.assert_awaited_once_with()

    async def test_partial_health_start_failure_closes_all_resources(self):
        self.health.start.side_effect = RuntimeError("health start failed")
        with self.assertRaisesRegex(RuntimeError, "health start failed"):
            await self.runner.main()
        self.health.close.assert_awaited_once_with()
        self.runner.close_client.assert_awaited_once_with()
        self.runner.engine.dispose.assert_awaited_once_with()

    async def test_baseline_only_success_uses_common_cleanup_without_health(self):
        with patch.object(self.runner.argparse.ArgumentParser, "parse_args", return_value=SimpleNamespace(baseline_only=True)):
            self.runner.initialize_badge_baseline = AsyncMock(return_value=2)
            await self.runner.main()
        self.runner.NotificationWorkerHealth.assert_not_called()
        self.runner.close_client.assert_awaited_once_with()
        self.runner.engine.dispose.assert_awaited_once_with()

    async def test_missing_baseline_does_not_skip_client_and_database_cleanup(self):
        self.session.get.return_value = None
        with self.assertRaisesRegex(RuntimeError, "complete badge baseline"):
            await self.runner.main()
        self.runner.close_client.assert_awaited_once_with()
        self.runner.engine.dispose.assert_awaited_once_with()

    async def test_baseline_only_cancellation_propagates_after_common_cleanup(self):
        entered = asyncio.Event()
        async def blocked_baseline():
            entered.set()
            await asyncio.Event().wait()
        self.runner.initialize_badge_baseline = AsyncMock(side_effect=blocked_baseline)
        with patch.object(self.runner.argparse.ArgumentParser, "parse_args", return_value=SimpleNamespace(baseline_only=True)):
            task = asyncio.create_task(self.runner.main())
            await asyncio.wait_for(entered.wait(), 2)
            self.callbacks[signal.SIGTERM]()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.runner.NotificationWorkerHealth.assert_not_called()
        self.runner.close_client.assert_awaited_once_with()
        self.runner.engine.dispose.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
