from datetime import datetime, timedelta, timezone
import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from app.api import admin as admin_api
from app.email.templates import email_verification_template
from app.utils.email_verification import hash_verification_code


CODE = "742091"
TOKEN = "owned-legacy-verification-token"


def unverified_user(*, code_required: bool = True):
    return SimpleNamespace(
        id=uuid4(),
        username="owned-verification-user",
        email="owned-verification@example.invalid",
        email_verified=False,
        email_verify_code_required=code_required,
        email_verify_token="previous-link" if not code_required else None,
        email_verify_expires=None,
        email_verify_code_hash="previous-code-hash" if code_required else None,
        email_verify_code_expires=None,
        email_verify_code_sent_at=None,
        email_verify_code_attempts=3 if code_required else 0,
    )


class Session:
    def __init__(self, user, events, *, commit_errors=()):
        self.user = user
        self.events = events
        self.commit_errors = list(commit_errors)
        self.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: user))
        self.commit = AsyncMock(side_effect=self._commit)
        self.rollback = AsyncMock(side_effect=lambda: events.append("rollback"))

    def _commit(self):
        self.events.append("commit")
        if self.commit_errors:
            error = self.commit_errors.pop(0)
            if error is not None:
                raise error


class AdminVerificationResendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.admin = SimpleNamespace(id=uuid4(), role="admin")
        self.request = SimpleNamespace()
        self.audit = self.enterContext(patch.object(admin_api, "record_audit", new=AsyncMock(side_effect=self._audit)))
        self.send = self.enterContext(patch("app.email.sender.send_email", new=AsyncMock(side_effect=self._send)))
        self.enterContext(patch("app.utils.email_verification.generate_verification_code", return_value=CODE))
        self.enterContext(patch("secrets.token_urlsafe", return_value=TOKEN))
        self.enterContext(patch.object(admin_api.settings, "jwt_secret_key", "unit-test-only-verification-secret"))
        self.enterContext(patch.object(admin_api.settings, "frontend_url", "https://owned.example.invalid/ctf/ko"))
        self.enterContext(patch.object(admin_api.settings, "email_verify_expire_hours", 3))
        self.enterContext(patch.object(admin_api.settings, "email_verify_code_expire_minutes", 7))
        self.enterContext(patch.object(admin_api.logger, "exception"))
        self.enterContext(patch.object(admin_api.logger, "warning"))

    async def _audit(self, _db, **fields):
        self.events.append("audit:" + fields["action"])

    async def _send(self, *_args, **_kwargs):
        self.events.append("send")

    async def invoke(self, target, db):
        return await admin_api.resend_verification_email(
            str(target.id) if target else str(uuid4()),
            request=self.request,
            admin=self.admin,
            db=db,
        )

    def assert_audit(self, target, method):
        self.audit.assert_awaited_once_with(
            ANY,
            actor_id=str(self.admin.id),
            action="user.resend_verification",
            target_type="user",
            target_id=str(target.id),
            details={"username": target.username, "method": method},
            request=self.request,
        )

    async def test_code_resend_issues_scoped_code_and_commits_audit_before_mail(self):
        target = unverified_user()
        target.email_verify_token = "obsolete-link"
        target.email_verify_expires = datetime.now(timezone.utc)
        db = Session(target, self.events)
        before = datetime.now(timezone.utc)
        result = await self.invoke(target, db)
        after = datetime.now(timezone.utc)

        self.assertEqual(result, {"message": "인증 메일을 재발송했습니다."})
        self.assertFalse(target.email_verified)
        self.assertTrue(target.email_verify_code_required)
        self.assertIsNone(target.email_verify_token)
        self.assertIsNone(target.email_verify_expires)
        self.assertEqual(target.email_verify_code_hash, hash_verification_code(target.id, CODE))
        self.assertNotEqual(target.email_verify_code_hash, CODE)
        self.assertEqual(target.email_verify_code_attempts, 0)
        self.assertLessEqual(before, target.email_verify_code_sent_at)
        self.assertLessEqual(target.email_verify_code_sent_at, after)
        self.assertEqual(target.email_verify_code_expires, target.email_verify_code_sent_at + timedelta(minutes=7))
        self.assertIsNotNone(db.execute.call_args.args[0]._for_update_arg)
        self.assert_audit(target, "code")
        self.send.assert_awaited_once_with(target.email, "[CTF Platform] 이메일 인증 코드", email_verification_template(target.username, verification_code=CODE))
        self.assertEqual(self.events, ["audit:user.resend_verification", "commit", "send"])
        db.rollback.assert_not_awaited()

    async def test_legacy_resend_replaces_link_and_uses_aware_configured_expiry(self):
        target = unverified_user(code_required=False)
        db = Session(target, self.events)
        before = datetime.now(timezone.utc)
        result = await self.invoke(target, db)
        after = datetime.now(timezone.utc)

        self.assertEqual(result, {"message": "인증 메일을 재발송했습니다."})
        self.assertEqual(target.email_verify_token, TOKEN)
        self.assertEqual(target.email_verify_expires.tzinfo, timezone.utc)
        self.assertLessEqual(before + timedelta(hours=3), target.email_verify_expires)
        self.assertLessEqual(target.email_verify_expires, after + timedelta(hours=3))
        self.assertFalse(target.email_verify_code_required)
        self.assertIsNone(target.email_verify_code_hash)
        self.assert_audit(target, "legacy_link")
        url = "https://owned.example.invalid/ctf/ko/verify-email?token=" + TOKEN
        self.send.assert_awaited_once_with(target.email, "[CTF Platform] 이메일 인증", email_verification_template(target.username, url))
        self.assertEqual(self.events, ["audit:user.resend_verification", "commit", "send"])
        db.rollback.assert_not_awaited()

    async def test_verified_user_is_400_without_audit_commit_or_mail(self):
        target = unverified_user()
        target.email_verified = True
        db = Session(target, self.events)
        with self.assertRaises(HTTPException) as caught:
            await self.invoke(target, db)
        self.assertEqual(caught.exception.status_code, 400)
        self.audit.assert_not_awaited()
        db.commit.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_missing_user_is_404_without_audit_commit_or_mail(self):
        db = Session(None, self.events)
        with self.assertRaises(HTTPException) as caught:
            await self.invoke(None, db)
        self.assertEqual(caught.exception.status_code, 404)
        self.audit.assert_not_awaited()
        db.commit.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_commit_failure_rolls_back_and_sends_no_mail_for_both_methods(self):
        for code_required in (True, False):
            with self.subTest(code_required=code_required):
                self.events.clear()
                self.audit.reset_mock()
                target = unverified_user(code_required=code_required)
                db = Session(target, self.events, commit_errors=[RuntimeError("synthetic commit failure")])
                with self.assertRaises(HTTPException) as caught:
                    await self.invoke(target, db)
                self.assertEqual(caught.exception.status_code, 500)
                self.assertEqual(caught.exception.detail, "인증 메일을 준비하지 못했습니다.")
                db.rollback.assert_awaited_once()
                self.send.assert_not_awaited()
                self.assertEqual(self.events, ["audit:user.resend_verification", "commit", "rollback"])

    async def test_audit_failure_rolls_back_before_commit_or_mail(self):
        target = unverified_user(code_required=False)
        db = Session(target, self.events)
        self.audit.side_effect = RuntimeError("synthetic audit failure")
        with self.assertRaises(HTTPException) as caught:
            await self.invoke(target, db)
        self.assertEqual(caught.exception.status_code, 500)
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_smtp_failure_keeps_prepared_state_and_records_failure_for_both_methods(self):
        async def fail_send(*_args, **_kwargs):
            self.events.append("send")
            raise RuntimeError("synthetic SMTP failure")

        self.send.side_effect = fail_send
        for code_required in (True, False):
            with self.subTest(code_required=code_required):
                self.events.clear()
                self.audit.reset_mock()
                self.send.reset_mock()
                target = unverified_user(code_required=code_required)
                db = Session(target, self.events)
                with self.assertRaises(HTTPException) as caught:
                    await self.invoke(target, db)
                self.assertEqual(caught.exception.status_code, 500)
                self.assertEqual(caught.exception.detail, "인증 메일 발송에 실패했습니다. SMTP 설정과 발송 로그를 확인하세요.")
                self.assertEqual(db.commit.await_count, 2)
                db.rollback.assert_not_awaited()
                self.send.assert_awaited_once()
                self.assertEqual(self.audit.await_count, 2)
                if code_required:
                    self.assertEqual(target.email_verify_code_hash, hash_verification_code(target.id, CODE))
                    self.assertIsNone(target.email_verify_token)
                    self.assertEqual(target.email_verify_code_attempts, 0)
                else:
                    self.assertEqual(target.email_verify_token, TOKEN)
                    self.assertIsNotNone(target.email_verify_expires)
                self.assertEqual(self.audit.await_args_list[1].kwargs, {
                    "actor_id": str(self.admin.id), "action": "auth.verification_delivery_failed",
                    "target_type": "user", "target_id": str(target.id),
                    "details": {"channel": "email", "admin_resend": True}, "request": self.request,
                })
                self.assertEqual(self.events, ["audit:user.resend_verification", "commit", "send", "audit:auth.verification_delivery_failed", "commit"])

    async def test_delivery_audit_commit_failure_still_returns_original_smtp_error_without_retry(self):
        target = unverified_user(code_required=False)
        db = Session(target, self.events, commit_errors=[None, RuntimeError("synthetic failure audit commit")])
        self.send.side_effect = RuntimeError("synthetic SMTP failure")
        with self.assertRaises(HTTPException) as caught:
            await self.invoke(target, db)
        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(caught.exception.detail, "인증 메일 발송에 실패했습니다. SMTP 설정과 발송 로그를 확인하세요.")
        self.assertEqual(db.commit.await_count, 2)
        db.rollback.assert_awaited_once()
        self.send.assert_awaited_once()

    async def test_existing_dependency_accepts_only_admin_role(self):
        dependency = inspect.signature(admin_api.resend_verification_email).parameters["admin"].default.dependency
        self.assertIs(await dependency(user=self.admin), self.admin)
        for role in ("moderator", "user", "professor", "student"):
            with self.subTest(role=role), self.assertRaises(HTTPException) as caught:
                await dependency(user=SimpleNamespace(role=role))
            self.assertEqual(caught.exception.status_code, 403)
        self.audit.assert_not_awaited()
        self.send.assert_not_awaited()
