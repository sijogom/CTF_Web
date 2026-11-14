import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.engine import make_url

import verify_rich_content_migration_postgres as harness


class _CreateFailureConnection:
    async def run_sync(self, callback, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("fixture create failed")


class _BeginContext:
    async def __aenter__(self) -> _CreateFailureConnection:
        return _CreateFailureConnection()

    async def __aexit__(self, exc_type, exc, traceback) -> bool:  # type: ignore[no-untyped-def]
        return False


class _CreateFailureEngine:
    url = make_url(
        "postgresql+asyncpg://fixture:secret@127.0.0.1:15432/ctf_acceptance_community"
    )

    def __init__(self) -> None:
        self.begin_calls = 0
        self.disposed = False

    def begin(self) -> _BeginContext:
        self.begin_calls += 1
        return _BeginContext()

    async def dispose(self) -> None:
        self.disposed = True


class RichContentMigrationPostgresHarnessTests(unittest.TestCase):
    def test_target_guard_requires_exact_owned_database_and_environment(self) -> None:
        exact_url = make_url(
            "postgresql+asyncpg://fixture:secret@127.0.0.1:15432/ctf_acceptance_community"
        )
        harness._assert_acceptance_target(
            database_url=exact_url,
            environment="development",
        )
        mismatches = (
            make_url("postgresql+asyncpg://u:p@localhost:15432/ctf_acceptance_community"),
            make_url("postgresql+asyncpg://u:p@127.0.0.1:5432/ctf_acceptance_community"),
            make_url("postgresql+asyncpg://u:p@127.0.0.1:15432/community"),
        )
        for url in mismatches:
            with self.subTest(url=url.render_as_string(hide_password=True)):
                with self.assertRaises(RuntimeError):
                    harness._assert_acceptance_target(database_url=url)
        with self.assertRaises(RuntimeError):
            harness._assert_acceptance_target(environment="production")

    def test_create_failure_never_enters_drop_cleanup(self) -> None:
        fake_engine = _CreateFailureEngine()
        verification_root = Path(__file__).resolve().parents[2] / ".verification"
        verification_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=verification_root) as directory:
            with (
                patch.object(harness, "engine", fake_engine),
                patch.object(harness.settings, "environment", "development"),
            ):
                with self.assertRaisesRegex(RuntimeError, "fixture create failed"):
                    asyncio.run(harness._run_contract(Path(directory)))
        self.assertEqual(fake_engine.begin_calls, 1)
        self.assertTrue(fake_engine.disposed)


if __name__ == "__main__":
    unittest.main()
