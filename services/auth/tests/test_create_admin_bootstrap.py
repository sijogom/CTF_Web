"""명시적 관리자 설정과 기존 계정 보존을 외부 연결 없이 검증한다."""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4


SERVICE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVICE_ROOT / "app" / "scripts" / "create_admin.py"
MAIN_PATH = SERVICE_ROOT / "app" / "main.py"
TEST_EMAIL = "bootstrap-unit@example.invalid"
TEST_USERNAME = "bootstrap-unit"
TEST_PASSWORD = "Owned-bootstrap-unit-Aa1!"
INVALID_PASSWORDS = (
    None, "", " \t\n", "changeme", " CHANGE_ME ", "replace_me",
    "your-admin-password", "placeholder", "changeme_auth",
    "<ADMIN_PASSWORD>", "${ADMIN_PASSWORD}",
)


class FakeExpression:
    def __init__(self, value):
        self.value = value

    def __or__(self, other):
        return FakeExpression(("or", self.value, other.value))


class FakeColumn:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return FakeExpression(("equals", self.name, value))


class FakeUser:
    role = FakeColumn("role")
    username = FakeColumn("username")
    email = FakeColumn("email")

    def __init__(self, **values):
        vars(self).update(values)


class FakeQuery:
    def __init__(self, model):
        self.model = model
        self.conditions = []

    def where(self, condition):
        self.conditions.append(condition.value)
        return self


class FakeSessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *args):
        return False


def load_script(*, password=None, existing=None):
    """제품 함수를 로드하되 DB·해시·설정 모듈은 작업 소유 fake로만 연결한다."""
    db = types.SimpleNamespace(
        execute=AsyncMock(return_value=types.SimpleNamespace(scalar_one_or_none=Mock(return_value=existing))),
        add=Mock(),
        commit=AsyncMock(),
    )
    session = Mock(return_value=FakeSessionContext(db))
    password_context = types.SimpleNamespace(verify=Mock(return_value=True), hash=Mock(return_value="unit-hash"))
    modules = {
        "sqlalchemy": types.SimpleNamespace(select=FakeQuery),
        "app.database": types.SimpleNamespace(async_session=session),
        "app.models.user": types.SimpleNamespace(User=FakeUser),
        "app.security": types.SimpleNamespace(pwd_context=password_context),
    }
    environment = {"ADMIN_EMAIL": TEST_EMAIL, "ADMIN_USERNAME": TEST_USERNAME}
    if password is not None:
        environment["ADMIN_PASSWORD"] = password
    spec = importlib.util.spec_from_file_location(f"_admin_bootstrap_unit_{uuid4().hex}", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, environment, clear=True), patch.dict("sys.modules", modules):
        spec.loader.exec_module(module)
    return types.SimpleNamespace(module=module, db=db, session=session, password_context=password_context)


def existing_user():
    return FakeUser(
        email="previous-unit@example.invalid", username="previous-unit", password_hash="previous-unit-hash",
        role="admin", is_active=False, email_verified=False,
    )


class AdminPasswordConfigurationTests(unittest.TestCase):
    def test_missing_environment_has_no_password_fallback(self):
        harness = load_script()
        self.assertIsNone(harness.module.DEFAULTS["password"])
        harness.session.assert_not_called()

    def test_environment_is_explicit_and_cli_overridable(self):
        harness = load_script(password=TEST_PASSWORD)
        self.assertEqual(harness.module.DEFAULTS["password"], TEST_PASSWORD)
        harness.session.assert_not_called()

    def test_missing_whitespace_and_placeholders_are_invalid(self):
        harness = load_script()
        for password in INVALID_PASSWORDS:
            with self.subTest(case=INVALID_PASSWORDS.index(password)):
                self.assertFalse(harness.module.is_configured_admin_password(password))
        harness.session.assert_not_called()

    def test_explicit_password_is_not_trimmed_or_replaced(self):
        harness = load_script()
        self.assertTrue(harness.module.is_configured_admin_password(TEST_PASSWORD))
        self.assertTrue(harness.module.is_configured_admin_password(f" {TEST_PASSWORD} "))

    def test_cli_missing_and_invalid_configuration_fails_before_database(self):
        for password in INVALID_PASSWORDS:
            with self.subTest(case=INVALID_PASSWORDS.index(password)):
                harness = load_script(password=password)
                output = io.StringIO()
                with redirect_stderr(output), self.assertRaises(SystemExit) as caught:
                    harness.module.main([])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("ADMIN_PASSWORD", output.getvalue())
                harness.session.assert_not_called()
                harness.password_context.hash.assert_not_called()
                harness.password_context.verify.assert_not_called()

    def test_cli_explicit_password_overrides_placeholder_environment(self):
        harness = load_script(password="changeme")
        output = io.StringIO()
        with redirect_stdout(output):
            harness.module.main(["--password", TEST_PASSWORD])
        harness.password_context.hash.assert_called_once_with(TEST_PASSWORD)
        harness.db.commit.assert_awaited_once_with()
        self.assertNotIn(TEST_PASSWORD, output.getvalue())

    def test_cli_empty_argument_does_not_fall_back_to_valid_environment(self):
        harness = load_script(password=TEST_PASSWORD)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            harness.module.main(["--password", ""])
        self.assertEqual(caught.exception.code, 2)
        harness.session.assert_not_called()

    def test_cli_environment_preserves_existing_manual_update_behavior(self):
        user = existing_user()
        harness = load_script(password=TEST_PASSWORD, existing=user)
        harness.password_context.verify.return_value = False
        output = io.StringIO()
        with redirect_stdout(output):
            harness.module.main([])
        self.assertEqual(user.email, TEST_EMAIL)
        self.assertEqual(user.username, TEST_USERNAME)
        self.assertEqual(user.password_hash, "unit-hash")
        harness.password_context.verify.assert_called_once_with(TEST_PASSWORD, "previous-unit-hash")
        harness.db.commit.assert_awaited_once_with()
        self.assertNotIn(TEST_PASSWORD, output.getvalue())


class CreateAdminBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_password_never_opens_database_or_hashes(self):
        for password in INVALID_PASSWORDS:
            with self.subTest(case=INVALID_PASSWORDS.index(password)):
                harness = load_script()
                with redirect_stdout(io.StringIO()), self.assertRaises(ValueError) as caught:
                    await harness.module.create_admin(TEST_EMAIL, TEST_USERNAME, password)
                self.assertIn("ADMIN_PASSWORD", str(caught.exception))
                harness.session.assert_not_called()
                harness.db.execute.assert_not_awaited()
                harness.db.commit.assert_not_awaited()
                harness.password_context.verify.assert_not_called()
                harness.password_context.hash.assert_not_called()

    async def test_missing_identity_keeps_existing_no_database_boundary(self):
        for email, username in (("", TEST_USERNAME), (TEST_EMAIL, "")):
            harness = load_script()
            with redirect_stdout(io.StringIO()):
                await harness.module.create_admin(email, username, TEST_PASSWORD)
            harness.session.assert_not_called()
            harness.password_context.hash.assert_not_called()

    async def test_new_admin_creation_keeps_flags_hash_and_single_commit(self):
        harness = load_script()
        with redirect_stdout(io.StringIO()):
            await harness.module.create_admin(TEST_EMAIL, TEST_USERNAME, TEST_PASSWORD)
        harness.session.assert_called_once_with()
        query = harness.db.execute.await_args.args[0]
        self.assertIs(query.model, FakeUser)
        self.assertEqual(query.conditions, [
            ("equals", "role", "admin"),
            ("or", ("equals", "username", TEST_USERNAME), ("equals", "email", TEST_EMAIL)),
        ])
        harness.password_context.hash.assert_called_once_with(TEST_PASSWORD)
        harness.password_context.verify.assert_not_called()
        created = harness.db.add.call_args.args[0]
        self.assertEqual(vars(created), {"email": TEST_EMAIL, "username": TEST_USERNAME,
            "password_hash": "unit-hash", "role": "admin", "is_active": True, "email_verified": True})
        harness.db.commit.assert_awaited_once_with()

    async def test_existing_unchanged_admin_does_not_hash_or_commit(self):
        user = existing_user()
        user.email, user.username = TEST_EMAIL, TEST_USERNAME
        before = vars(user).copy()
        harness = load_script(existing=user)
        with redirect_stdout(io.StringIO()):
            await harness.module.create_admin(TEST_EMAIL, TEST_USERNAME, TEST_PASSWORD)
        self.assertEqual(vars(user), before)
        harness.password_context.verify.assert_called_once_with(TEST_PASSWORD, before["password_hash"])
        harness.password_context.hash.assert_not_called()
        harness.db.commit.assert_not_awaited()
        harness.db.add.assert_not_called()

    async def test_existing_manual_update_keeps_role_activation_and_commit_behavior(self):
        user = existing_user()
        harness = load_script(existing=user)
        harness.password_context.verify.return_value = False
        with redirect_stdout(io.StringIO()):
            await harness.module.create_admin(TEST_EMAIL, TEST_USERNAME, TEST_PASSWORD)
        self.assertEqual(vars(user), {"email": TEST_EMAIL, "username": TEST_USERNAME,
            "password_hash": "unit-hash", "role": "admin", "is_active": False, "email_verified": False})
        harness.password_context.verify.assert_called_once_with(TEST_PASSWORD, "previous-unit-hash")
        harness.password_context.hash.assert_called_once_with(TEST_PASSWORD)
        harness.db.commit.assert_awaited_once_with()
        harness.db.add.assert_not_called()

    async def test_create_only_existing_admin_never_changes_profile_hash_or_commit(self):
        user = existing_user()
        before = vars(user).copy()
        harness = load_script(existing=user)
        output = io.StringIO()
        with redirect_stdout(output):
            await harness.module.create_admin(TEST_EMAIL, TEST_USERNAME, TEST_PASSWORD, update_existing=False)
        self.assertEqual(vars(user), before)
        harness.password_context.verify.assert_not_called()
        harness.password_context.hash.assert_not_called()
        harness.db.commit.assert_not_awaited()
        harness.db.add.assert_not_called()
        self.assertNotIn(TEST_PASSWORD, output.getvalue())

    async def test_create_only_creates_missing_admin_without_changing_creation_contract(self):
        harness = load_script()
        with redirect_stdout(io.StringIO()):
            await harness.module.create_admin(TEST_EMAIL, TEST_USERNAME, f" {TEST_PASSWORD} ", update_existing=False)
        harness.password_context.hash.assert_called_once_with(f" {TEST_PASSWORD} ")
        harness.db.commit.assert_awaited_once_with()
        self.assertEqual(harness.db.add.call_args.args[0].role, "admin")

    async def test_lookup_failure_does_not_create_or_commit(self):
        harness = load_script()
        harness.db.execute.side_effect = RuntimeError("owned lookup failure")
        with self.assertRaisesRegex(RuntimeError, "owned lookup failure"):
            await harness.module.create_admin(TEST_EMAIL, TEST_USERNAME, TEST_PASSWORD)
        harness.password_context.hash.assert_not_called()
        harness.db.add.assert_not_called()
        harness.db.commit.assert_not_awaited()


class ApiStartupBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def run_lifespan(self, password):
        harness = load_script()
        create_admin = AsyncMock()
        bootstrap_module = types.SimpleNamespace(create_admin=create_admin,
            is_configured_admin_password=harness.module.is_configured_admin_password)
        rehydrate = AsyncMock(return_value=True)
        engine = types.SimpleNamespace(dispose=AsyncMock())
        close_redis, close_client = AsyncMock(), AsyncMock()
        logger = Mock()
        settings = types.SimpleNamespace(jwt_algorithm="HS256", admin_email=TEST_EMAIL,
            admin_username=TEST_USERNAME, admin_password=password)
        module_ast = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
        lifespan = next(node for node in module_ast.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan")
        namespace = {"asynccontextmanager": asynccontextmanager, "FastAPI": object, "settings": settings,
            "logger": logger, "engine": engine, "close_redis": close_redis, "close_client": close_client}
        exec(compile(ast.Module(body=[lifespan], type_ignores=[]), str(MAIN_PATH), "exec"), namespace)
        with patch.dict("sys.modules", {"app.utils.admin_settings": types.SimpleNamespace(rehydrate_admin_settings_cache=rehydrate),
            "app.scripts.create_admin": bootstrap_module}):
            async with namespace["lifespan"](object()):
                rehydrate.assert_awaited_once_with()
        engine.dispose.assert_awaited_once_with()
        close_redis.assert_awaited_once_with()
        close_client.assert_awaited_once_with()
        harness.session.assert_not_called()
        return create_admin, logger

    async def test_missing_configuration_skips_bootstrap_but_starts_and_closes_service(self):
        for password in INVALID_PASSWORDS:
            with self.subTest(case=INVALID_PASSWORDS.index(password)):
                create_admin, logger = await self.run_lifespan(password)
                create_admin.assert_not_awaited()
                logger.warning.assert_called_once()
                self.assertIn("ADMIN_PASSWORD", logger.warning.call_args.args[0])

    async def test_explicit_configuration_calls_create_only_with_exact_existing_arguments(self):
        create_admin, logger = await self.run_lifespan(TEST_PASSWORD)
        create_admin.assert_awaited_once_with(email=TEST_EMAIL, username=TEST_USERNAME,
            password=TEST_PASSWORD, update_existing=False)
        logger.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
