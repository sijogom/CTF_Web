"""Run from each service's isolated environment with that service on PYTHONPATH."""

import importlib
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import Settings, settings
from app.middleware.security_headers import SecurityHeadersMiddleware


_SERVICE = os.environ.get(
    "CTF_TEST_SERVICE", Path(importlib.import_module("app.config").__file__).parents[1].name
)
if _SERVICE not in {"auth", "learn", "community"}:
    raise RuntimeError("CTF_TEST_SERVICE must identify auth, learn, or community")


class _JsonPayload(BaseModel):
    title: str
    count: int


class FrameworkSecurityCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upload_handles = []
        self.app = FastAPI()

        @self.app.post("/json", response_model=_JsonPayload)
        async def json_body(body: _JsonPayload):
            return body

        @self.app.post("/form")
        async def form_body(title: str = Form(...), count: int = Form(...)):
            return {"title": title, "count": count}

        @self.app.post("/bounded-form")
        async def bounded_form(request: Request):
            async with request.form(max_fields=2, max_part_size=16) as form:
                return {"fields": list(form.multi_items())}

        @self.app.post("/upload")
        async def upload(title: str = Form(...), file: UploadFile = File(...)):
            self.upload_handles.append(file)
            contents = await file.read()
            return {"title": title, "name": file.filename, "size": len(contents)}

        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["https://ctf.example.invalid"],
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "Authorization", "X-CSRF-Token"],
        )
        self.app.add_middleware(SecurityHeadersMiddleware, is_production=True)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://test.invalid"
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_json_validation_and_unicode_response_are_preserved(self):
        response = await self.client.post("/json", json={"title": "\ud55c\uae00", "count": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"title": "\ud55c\uae00", "count": 2})
        invalid = await self.client.post("/json", json={"title": "x", "count": "invalid"})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["detail"][0]["loc"], ["body", "count"])

    async def test_json_without_json_content_type_is_rejected(self):
        body = json.dumps({"title": "x", "count": 2})
        for headers in ({}, {"content-type": "text/plain"}):
            response = await self.client.post("/json", content=body, headers=headers)
            self.assertEqual(response.status_code, 422)

    async def test_urlencoded_form_coercion_and_unicode_are_preserved(self):
        response = await self.client.post("/form", data={"title": "\ud55c\uae00 \uacf5\ubc31", "count": "3"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"title": "\ud55c\uae00 \uacf5\ubc31", "count": 3})

    async def test_urlencoded_duplicate_fields_keep_multi_items(self):
        response = await self.client.post(
            "/bounded-form", content="tag=a&tag=b",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["fields"], [["tag", "a"], ["tag", "b"]])

    async def test_urlencoded_field_count_limit_is_enforced_during_parsing(self):
        response = await self.client.post(
            "/bounded-form", content="a=1&b=2&c=3",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Too many fields", response.json()["detail"])

    async def test_urlencoded_field_size_limit_is_enforced_during_parsing(self):
        for body in ("a=" + "x" * 17, urlencode({"a": "\ud55c\uae00" * 9})):
            response = await self.client.post(
                "/bounded-form", content=body,
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("maximum size", response.json()["detail"])

    async def test_fastapi_default_urlencoded_field_limits_are_enforced(self):
        for body in ("&".join(f"field{i}=x" for i in range(1001)), "title=" + "x" * (1024 * 1024 + 1)):
            response = await self.client.post(
                "/form", content=body,
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(response.status_code, 400)

    async def test_multipart_file_spools_and_closes_without_losing_form_fields(self):
        response = await self.client.post(
            "/upload", data={"title": "\ud55c\uae00"},
            files={"file": ("sample.txt", b"a" * (1024 * 1024 + 16), "text/plain")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"title": "\ud55c\uae00", "name": "sample.txt", "size": 1024 * 1024 + 16})
        self.assertTrue(self.upload_handles[0].file.closed)

    async def test_multipart_nonfile_part_limit_is_enforced(self):
        response = await self.client.post(
            "/upload", files={"title": (None, "x" * (1024 * 1024 + 1)), "file": ("sample.txt", b"x")},
        )
        self.assertEqual(response.status_code, 400)

    async def test_credentialed_cors_and_security_headers_are_preserved(self):
        allowed = await self.client.options("/json", headers={
            "origin": "https://ctf.example.invalid", "access-control-request-method": "POST",
            "access-control-request-headers": "content-type,x-csrf-token",
        })
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers["access-control-allow-origin"], "https://ctf.example.invalid")
        self.assertEqual(allowed.headers["access-control-allow-credentials"], "true")
        self.assertEqual(allowed.headers["x-content-type-options"], "nosniff")
        self.assertEqual(allowed.headers["x-frame-options"], "DENY")
        self.assertIn("max-age=31536000", allowed.headers["strict-transport-security"])
        denied = await self.client.options("/json", headers={
            "origin": "https://foreign.example.invalid", "access-control-request-method": "POST",
        })
        self.assertEqual(denied.status_code, 400)
        self.assertNotIn("access-control-allow-origin", denied.headers)


class ServiceSecurityCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = _SERVICE
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private_key = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode("ascii")
        cls.public_key = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("ascii")
        cls.payload = {
            "sub": str(uuid4()), "type": "access", "role": "user", "username": "compat-user",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        }

    def _decode(self, token):
        with patch.object(settings, "jwt_algorithm", "RS256"), patch.object(settings, "jwt_public_key", self.public_key):
            if self.service == "auth":
                return importlib.import_module("app.jwt.token").decode_token(token)
            request = Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))]})
            return importlib.import_module("app.api.deps")._decode_bearer_token(request)

    def test_service_accepts_real_rs256_access_token(self):
        token = jwt.encode(self.payload, self.private_key, algorithm="RS256")
        result = self._decode(token)
        self.assertEqual(result["sub"], self.payload["sub"])
        self.assertEqual(result["username"], "compat-user")

    def test_service_rejects_unsigned_hmac_expired_and_wrong_subject_tokens(self):
        invalid_tokens = [
            jwt.encode(self.payload, "", algorithm="none"),
            jwt.encode(self.payload, "compatibility-only-hmac-secret-32-bytes", algorithm="HS256"),
            jwt.encode({**self.payload, "exp": datetime.now(timezone.utc) - timedelta(seconds=5)}, self.private_key, algorithm="RS256"),
            jwt.encode({**self.payload, "sub": 123}, self.private_key, algorithm="RS256"),
        ]
        for token in invalid_tokens:
            with self.subTest(token_kind=jwt.get_unverified_header(token)["alg"]):
                self.assertIsNone(self._decode(token))

    def test_service_token_type_and_cookie_precedence_are_preserved(self):
        if self.service == "auth":
            module = importlib.import_module("app.jwt.token")
            with patch.object(settings, "jwt_algorithm", "RS256"), patch.object(settings, "jwt_secret_key", self.private_key), patch.object(settings, "jwt_public_key", self.public_key):
                access = module.decode_token(module.create_access_token(self.payload["sub"], "user", "compat-user"))
                refresh = module.decode_token(module.create_refresh_token(self.payload["sub"]))
                self.assertEqual(access["type"], "access")
                self.assertEqual(refresh["type"], "refresh")
                self.assertTrue(refresh["jti"])
            return
        module = importlib.import_module("app.api.deps")
        refresh = jwt.encode({**self.payload, "type": "refresh"}, self.private_key, algorithm="RS256")
        self.assertIsNone(self._decode(refresh))
        access = jwt.encode(self.payload, self.private_key, algorithm="RS256")
        request = Request({"type": "http", "headers": [
            (b"cookie", b"access_token=invalid"), (b"authorization", f"Bearer {access}".encode("ascii")),
        ]})
        with patch.object(settings, "jwt_algorithm", "RS256"), patch.object(settings, "jwt_public_key", self.public_key):
            self.assertIsNone(module._decode_bearer_token(request))

    def test_settings_load_valid_values_and_reject_unsafe_production_inline_keys(self):
        config = Settings(_env_file=None, jwt_algorithm="HS256", jwt_public_key="compatibility-only-public-value")
        self.assertEqual(config.jwt_algorithm, "HS256")
        with self.assertRaises(ValueError):
            Settings(_env_file=None, environment="production", jwt_algorithm="RS256", jwt_public_key=self.public_key)

    def test_service_registration_and_openapi_work_without_running_lifespan(self):
        module = importlib.import_module("app.main")
        schema = module.app.openapi()
        self.assertTrue(schema["paths"])
        self.assertTrue(any("application/json" in str(operation) for path in schema["paths"].values() for operation in path.values()))

    def test_existing_fernet_contract_remains_readable(self):
        from cryptography.fernet import Fernet
        if self.service == "auth":
            module = importlib.import_module("app.utils.crypto")
            with patch.object(settings, "jwt_secret_key", "compatibility-only-secret"):
                legacy = Fernet(module._derive_key_legacy()).encrypt(b"preserved-data").decode("ascii")
                self.assertEqual(module.decrypt_value(legacy), "preserved-data")
                self.assertEqual(module.decrypt_value(module.encrypt_value("preserved-data")), "preserved-data")
        elif self.service == "learn":
            module = importlib.import_module("app.utils.flag_audit")
            key = Fernet.generate_key().decode("ascii")
            encrypted = module.encrypt_challenge_submission_flag("CTF{compatibility}", encryption_key=key)
            self.assertEqual(module.decrypt_challenge_submission_flag(encrypted, encryption_key=key), "CTF{compatibility}")
            with self.assertRaises(module.FlagStorageUnavailable):
                module.decrypt_challenge_submission_flag(encrypted, encryption_key=Fernet.generate_key().decode("ascii"))
        else:
            fernet = Fernet(Fernet.generate_key())
            self.assertEqual(fernet.decrypt(fernet.encrypt(b"preserved-data")), b"preserved-data")


@unittest.skipUnless(_SERVICE == "auth", "Auth-only CSRF and rate-limit middleware")
class AuthMiddlewareCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.middleware.csrf import CSRFMiddleware
        from app.middleware.rate_limit import RateLimitMiddleware

        self.rate_limit = importlib.import_module("app.middleware.rate_limit")
        self.rate_patch = patch.object(self.rate_limit, "redis_client", None)
        self.counters_patch = patch.dict(self.rate_limit._memory_counters, {}, clear=True)
        self.rate_patch.start()
        self.counters_patch.start()
        self.app = FastAPI()

        @self.app.post("/write")
        async def write(body: _JsonPayload):
            return body

        @self.app.post("/login")
        async def login(title: str = Form(...)):
            return {"title": title}

        self.app.add_middleware(CSRFMiddleware)
        self.app.add_middleware(RateLimitMiddleware)
        self.app.add_middleware(SecurityHeadersMiddleware)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://test.invalid"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.counters_patch.stop()
        self.rate_patch.stop()

    async def test_csrf_guard_and_base_middleware_preserve_valid_json_body(self):
        from app.utils.cookies import CSRF_COOKIE

        safe = await self.client.get("/write")
        self.assertEqual(safe.status_code, 405)
        csrf = safe.cookies[CSRF_COOKIE]
        denied = await self.client.post("/write", json={"title": "x", "count": 1})
        self.assertEqual(denied.status_code, 403)
        allowed = await self.client.post(
            "/write", json={"title": "\ud55c\uae00", "count": 2},
            headers={"cookie": f"{CSRF_COOKIE}={csrf}", "x-csrf-token": csrf},
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json(), {"title": "\ud55c\uae00", "count": 2})
        self.assertEqual(allowed.headers["x-frame-options"], "DENY")

    async def test_memory_rate_limit_preserves_form_body_and_returns_retry_after(self):
        for _ in range(5):
            response = await self.client.post("/login", data={"title": "\ud55c\uae00"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"title": "\ud55c\uae00"})
        limited = await self.client.post("/login", data={"title": "\ud55c\uae00"})
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["retry-after"], "60")


if __name__ == "__main__":
    unittest.main(verbosity=2)
