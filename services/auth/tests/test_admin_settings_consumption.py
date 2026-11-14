import json
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from fastapi import HTTPException

from app.api import admin as admin_api
from app.api import public_settings as public_settings_api
from app.api import register as register_api
from app.api.admin import AdminSettingsRequest
from app.email import sender
from app.schemas.auth import RegisterRequest
from app.utils import admin_settings
from app.utils.admin_settings import (
    ADMIN_SETTINGS_UNAVAILABLE_DETAIL,
    AdminSettingsStorageError,
)


class _DatabaseResult:
    def __init__(self, value: str | None):
        self.value = value

    def scalar_one_or_none(self) -> str | None:
        return self.value


class _DatabaseStub:
    def __init__(self, value: str | None = None):
        self.execute = AsyncMock(return_value=_DatabaseResult(value))
        self.commit = AsyncMock()
        self.rollback = AsyncMock()


class _RedisStub:
    def __init__(self, *, set_error: Exception | None = None):
        self.get = AsyncMock(return_value=None)
        self.set = AsyncMock()
        if set_error is not None:
            self.set.side_effect = set_error


class AdminSettingsReadPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_value_remains_authoritative_when_cache_refresh_fails(self):
        db = _DatabaseStub(json.dumps({"registration_open": False, "smtp_host": "smtp.example"}))
        redis = _RedisStub(set_error=RuntimeError("cache unavailable"))

        with patch.object(admin_settings, "redis_client", redis):
            values, source = await admin_settings.load_admin_settings(db)

        self.assertFalse(values["registration_open"])
        self.assertEqual(values["smtp_host"], "smtp.example")
        self.assertEqual(source, "database_cache_unavailable")
        redis.get.assert_not_awaited()
        redis.set.assert_awaited_once()

    async def test_public_settings_returns_only_normalized_site_name(self):
        persisted = {
            "site_name": "  Security Lab  ",
            "smtp_password": "must-not-leak",
            "registration_open": False,
        }
        loader = AsyncMock(return_value=(persisted, "database"))
        with patch.object(
            public_settings_api,
            "load_admin_settings",
            loader,
        ):
            response = await public_settings_api.get_public_settings(_DatabaseStub())

        self.assertEqual(response.model_dump(), {"site_name": "Security Lab"})
        loader.assert_awaited_once_with(ANY, refresh_cache=False)

    async def test_public_settings_storage_failure_is_stable_503(self):
        with patch.object(
            public_settings_api,
            "load_admin_settings",
            AsyncMock(side_effect=AdminSettingsStorageError("database internals")),
        ):
            with self.assertRaises(HTTPException) as raised:
                await public_settings_api.get_public_settings(_DatabaseStub())

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, ADMIN_SETTINGS_UNAVAILABLE_DETAIL)
        self.assertNotIn("database", raised.exception.detail)

    def test_site_name_update_is_trimmed_and_bounded(self):
        self.assertEqual(AdminSettingsRequest(site_name="  Security Lab  ").site_name, "Security Lab")
        with self.assertRaises(ValueError):
            AdminSettingsRequest(site_name="   ")
        with self.assertRaises(ValueError):
            AdminSettingsRequest(site_name="x" * 101)

    def test_empty_password_preserves_existing_encrypted_value(self):
        values = {
            "smtp_host": "smtp.example",
            "smtp_port": 587,
            "smtp_from": "noreply@example.com",
            "smtp_tls": True,
            "smtp_user": "mailer",
            "smtp_password": "",
        }

        with patch.object(admin_settings, "encrypt_value", return_value="encrypted") as encrypt:
            updates = admin_settings.prepare_smtp_settings_update(values)

        self.assertNotIn("smtp_password", updates)
        encrypt.assert_not_called()

    async def test_invalid_persisted_smtp_port_does_not_fall_back_to_environment(self):
        invalid_settings = {
            "smtp_host": "smtp.example",
            "smtp_port": 0,
            "smtp_from": "noreply@example.com",
            "smtp_tls": True,
            "smtp_user": "",
            "smtp_password": "",
        }

        with patch.object(sender, "load_runtime_admin_settings", AsyncMock(return_value=invalid_settings)):
            with self.assertRaises(sender.SmtpConfigurationError):
                await sender._get_smtp_config()


class RegistrationGateTests(unittest.IsolatedAsyncioTestCase):
    def _request(self) -> RegisterRequest:
        return RegisterRequest(
            email="student@cju.ac.kr",
            username="registration_gate_user",
            password="StrongPass1!",
        )

    async def test_closed_registration_rejects_before_user_lookup_or_creation(self):
        db = _DatabaseStub()
        with patch.object(
            register_api,
            "load_admin_settings",
            AsyncMock(return_value=({"registration_open": False}, "database")),
        ):
            with self.assertRaises(HTTPException) as raised:
                await register_api.register(self._request(), SimpleNamespace(), db)

        self.assertEqual(raised.exception.status_code, 403)
        db.execute.assert_not_awaited()

    async def test_registration_storage_error_uses_stable_public_message(self):
        db = _DatabaseStub()
        with patch.object(
            register_api,
            "load_admin_settings",
            AsyncMock(side_effect=AdminSettingsStorageError("internal database failure")),
        ):
            with self.assertRaises(HTTPException) as raised:
                await register_api.register(self._request(), SimpleNamespace(), db)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, ADMIN_SETTINGS_UNAVAILABLE_DETAIL)
        self.assertNotIn("internal", raised.exception.detail)


class SmtpCompatibilityAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_general_settings_smtp_update_uses_safe_audit_shape(self):
        db = _DatabaseStub()
        admin = SimpleNamespace(id="admin-id")
        request = SimpleNamespace()
        body = AdminSettingsRequest(
            smtp_host="smtp.example",
            smtp_port=587,
            smtp_from="noreply@example.com",
            smtp_tls=True,
            smtp_user="mailer",
            smtp_password="__UNCHANGED__",
        )
        smtp_updates = {
            "smtp_host": "smtp.example",
            "smtp_port": 587,
            "smtp_from": "noreply@example.com",
            "smtp_tls": True,
            "smtp_user": "mailer",
        }

        with (
            patch.object(admin_api, "prepare_smtp_settings_update", return_value=smtp_updates),
            patch.object(
                admin_api,
                "load_admin_settings",
                AsyncMock(return_value=({"registration_open": True, "smtp_password": "encrypted"}, "database")),
            ),
            patch.object(admin_api, "save_admin_settings", AsyncMock()) as save_settings,
            patch.object(admin_api, "record_audit", AsyncMock()) as record_audit,
            patch.object(admin_api, "refresh_admin_settings_cache", AsyncMock(return_value=True)),
        ):
            response = await admin_api.update_admin_settings(body, request, admin, db)

        self.assertFalse(response["cache_refresh_pending"])
        saved_values = save_settings.await_args.args[1]
        self.assertEqual(saved_values["smtp_password"], "encrypted")
        audit_kwargs = record_audit.await_args.kwargs
        self.assertEqual(audit_kwargs["action"], "settings.smtp_update")
        self.assertEqual(audit_kwargs["details"]["new_values"], {})
        self.assertNotIn("smtp_password", audit_kwargs["details"])


if __name__ == "__main__":
    unittest.main()
