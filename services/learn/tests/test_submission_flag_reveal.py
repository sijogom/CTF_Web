"""사유 필수 열람의 schema/HTTP/commit 및 additive migration 순수 계약."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.api import challenges, deps, submissions  # noqa: E402
from app.models.challenge import SubmissionFlagRevealAudit  # noqa: E402
from app.schemas.submission import SubmissionFlagRevealRequest  # noqa: E402


def stored_submission(**overrides):
    values = {
        "id": uuid4(),
        "submitted_flag": "[redacted]",
        "submitted_flag_storage_version": "encrypted-v1",
        "submitted_flag_ciphertext": b"synthetic-owned-ciphertext",
        "submitted_at": datetime(2026, 10, 18, 1, 2, 3, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def session(row):
    return SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: row)),
        add=Mock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )


class SubmissionFlagRevealSchemaTests(unittest.TestCase):
    def test_reason_reuses_trimmed_nonempty_learn_safe_text_contract(self):
        self.assertEqual(SubmissionFlagRevealRequest(reason="  승인된 확인  ").reason, "승인된 확인")
        self.assertEqual(len(SubmissionFlagRevealRequest(reason="가" * 1000).reason), 1000)
        for fields in (
            {}, {"reason": ""}, {"reason": " \t\n "}, {"reason": "가" * 1001},
            {"reason": "x\x00y"}, {"reason": "x\x7fy"}, {"reason": "<b>사유</b>"},
            {"reason": "<script>x</script>"}, {"reason": None}, {"reason": False},
            {"reason": "사유", "actor_id": str(uuid4())},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                SubmissionFlagRevealRequest(**fields)


class SubmissionFlagRevealRouteTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, db, row_id=None):
        return await submissions.reveal_submission_flag(
            row_id or uuid4(), SubmissionFlagRevealRequest(reason="  소유 제출 확인  "),
            db=db, actor_id=self.actor, _role="admin",
        )

    def setUp(self):
        self.actor = uuid4()
        self.decrypt = self.enterContext(patch.object(submissions, "decrypt_submitted_flag", return_value="owned-synthetic-value"))

    async def test_audit_has_exact_actor_submission_reason_before_success(self):
        row = stored_submission()
        db = session(row)
        events = []
        db.add.side_effect = lambda _audit: events.append("audit")
        db.commit.side_effect = lambda: events.append("commit")
        result = await self.invoke(db, row.id)
        events.append("response")
        audit = db.add.call_args.args[0]
        self.assertIsInstance(audit, SubmissionFlagRevealAudit)
        self.assertEqual((audit.submission_id, audit.actor_id, audit.reason), (row.id, self.actor, "소유 제출 확인"))
        self.assertNotIn("owned-synthetic-value", vars(audit).values())
        self.assertEqual(result, {"id": str(row.id), "submitted_flag": "owned-synthetic-value", "submitted_at": row.submitted_at.isoformat()})
        self.assertEqual(events, ["audit", "commit", "response"])
        db.rollback.assert_not_awaited()

    async def test_missing_submission_is_404_without_audit_or_decryption(self):
        db = session(None)
        with self.assertRaises(HTTPException) as caught:
            await self.invoke(db)
        self.assertEqual(caught.exception.status_code, 404)
        self.decrypt.assert_not_called()
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    async def test_unretained_or_undecryptable_value_is_409_without_audit(self):
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted):
                row = stored_submission(submitted_flag_ciphertext=b"broken" if encrypted else None)
                db = session(row)
                self.decrypt.side_effect = submissions.FlagStorageUnavailable("synthetic failure")
                with self.assertRaises(HTTPException) as caught:
                    await self.invoke(db)
                self.assertEqual(caught.exception.status_code, 409)
                db.add.assert_not_called()
                db.commit.assert_not_awaited()

    async def test_legacy_raw_value_keeps_same_audited_post_only_compatibility(self):
        row = stored_submission(submitted_flag_storage_version="legacy-raw", submitted_flag_ciphertext=None, submitted_flag="owned-legacy-value")
        db = session(row)
        result = await self.invoke(db)
        self.assertEqual(result["submitted_flag"], "owned-legacy-value")
        self.decrypt.assert_not_called()
        self.assertEqual(db.add.call_args.args[0].reason, "소유 제출 확인")
        db.commit.assert_awaited_once()

    async def test_audit_add_or_commit_failure_rolls_back_and_returns_no_plaintext(self):
        for failing_step in ("add", "commit"):
            with self.subTest(failing_step=failing_step):
                db = session(stored_submission())
                getattr(db, failing_step).side_effect = RuntimeError("synthetic audit failure")
                with self.assertRaises(HTTPException) as caught:
                    await self.invoke(db)
                self.assertEqual(caught.exception.status_code, 503)
                self.assertEqual(caught.exception.detail, "제출 원문 열람 감사 기록에 실패했습니다.")
                self.assertNotIn("owned-synthetic-value", str(caught.exception.detail))
                db.rollback.assert_awaited_once()
                if failing_step == "add":
                    db.commit.assert_not_awaited()

    async def request(self, method, *, role="admin", body=None):
        app = FastAPI()
        app.include_router(submissions.flag_logs_router)
        db = session(stored_submission())
        app.dependency_overrides[submissions.get_db] = lambda: db
        payload = {"sub": str(self.actor), "type": "access"} if role else deps._NO_TOKEN
        with patch.object(deps, "_decode_bearer_token", return_value=payload), patch.object(
            deps, "_require_current_authorization", new=AsyncMock(return_value=role)
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://owned.invalid") as client:
                response = await client.request(method, f"/submissions/logs/{uuid4()}/flag", json=body)
        return response, db

    async def test_http_get_is_guarded_405_allow_post_with_no_read_or_write(self):
        response, db = await self.request("GET")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers["allow"], "POST")
        self.assertNotIn("submitted_flag", response.json())
        self.decrypt.assert_not_called()
        db.execute.assert_not_awaited()
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    async def test_http_admin_post_requires_valid_body_and_uses_current_role_guard(self):
        for body in (None, {}, {"reason": " "}, {"reason": "사유", "unknown": True}):
            response, db = await self.request("POST", body=body)
            self.assertEqual(response.status_code, 422)
            db.execute.assert_not_awaited()
            db.commit.assert_not_awaited()
        response, db = await self.request("POST", body={"reason": "소유 제출 확인"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["submitted_flag"], "owned-synthetic-value")
        self.assertEqual(db.add.call_args.args[0].reason, "소유 제출 확인")
        db.commit.assert_awaited_once()

    async def test_http_moderator_and_anonymous_cannot_reveal_by_either_method(self):
        for role, expected in (("moderator", 403), (None, 401)):
            for method in ("GET", "POST"):
                with self.subTest(role=role, method=method):
                    response, db = await self.request(method, role=role, body={"reason": "소유 제출 확인"})
                    self.assertEqual(response.status_code, expected)
                    self.assertNotIn("submitted_flag", response.json())
                    db.execute.assert_not_awaited()
                    db.add.assert_not_called()
                    db.commit.assert_not_awaited()


class SubmissionRevealReasonMigrationTests(unittest.TestCase):
    def setUp(self):
        path = SERVICE_ROOT / "migrations" / "versions" / "20261018_030_submission_flag_reveal_reason.py"
        spec = importlib.util.spec_from_file_location("owned_reveal_reason_migration", path)
        self.migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.migration)

    def test_upgrade_is_nullable_additive_without_fabricated_history(self):
        with patch.object(self.migration.op, "add_column") as add:
            self.migration.upgrade()
        table, column = add.call_args.args
        self.assertEqual((self.migration.revision, self.migration.down_revision), ("20261018_030", "20261013_029"))
        self.assertEqual((table, column.name, column.nullable), ("submission_flag_reveal_audits", "reason", True))
        self.assertIsNone(column.server_default)
        self.assertIsNone(column.default)
        self.assertTrue(SubmissionFlagRevealAudit.__table__.c.reason.nullable)
        self.assertEqual(next(iter(SubmissionFlagRevealAudit.__table__.c.submission_id.foreign_keys)).ondelete, "CASCADE")

    def test_downgrade_with_any_reason_refuses_before_drop(self):
        bind = Mock()
        bind.execute.return_value.scalar_one.return_value = 1
        with patch.object(self.migration.op, "get_bind", return_value=bind), patch.object(self.migration.op, "drop_column") as drop:
            with self.assertRaisesRegex(RuntimeError, "열람 사유"):
                self.migration.downgrade()
        self.assertIn("reason IS NOT NULL", str(bind.execute.call_args.args[0]))
        self.assertEqual(str(bind.execute.call_args_list[0].args[0]), "LOCK TABLE submission_flag_reveal_audits IN ACCESS EXCLUSIVE MODE")
        self.assertEqual(bind.execute.call_count, 2)
        drop.assert_not_called()

    def test_downgrade_of_all_null_reason_drops_only_new_column(self):
        bind = Mock()
        bind.execute.return_value.scalar_one.return_value = 0
        with patch.object(self.migration.op, "get_bind", return_value=bind), patch.object(self.migration.op, "drop_column") as drop:
            self.migration.downgrade()
        drop.assert_called_once_with("submission_flag_reveal_audits", "reason")


    def test_downgrade_lock_count_and_drop_share_connection_and_exact_order(self):
        events = []
        bind = Mock()

        def execute(statement):
            events.append(str(statement))
            return SimpleNamespace(scalar_one=lambda: 0)

        bind.execute.side_effect = execute
        with patch.object(self.migration.op, "get_bind", return_value=bind) as get_bind, patch.object(
            self.migration.op, "drop_column", side_effect=lambda table, column: events.append((table, column))
        ):
            self.migration.downgrade()
        self.assertEqual(events, [
            "LOCK TABLE submission_flag_reveal_audits IN ACCESS EXCLUSIVE MODE",
            "SELECT count(*) FROM submission_flag_reveal_audits WHERE reason IS NOT NULL",
            ("submission_flag_reveal_audits", "reason"),
        ])
        get_bind.assert_called_once()
        bind.commit.assert_not_called()

    def test_downgrade_lock_timeout_stops_without_count_drop_or_retry(self):
        bind = Mock()
        bind.execute.side_effect = RuntimeError("synthetic lock timeout")
        with patch.object(self.migration.op, "get_bind", return_value=bind), patch.object(self.migration.op, "drop_column") as drop:
            with self.assertRaisesRegex(RuntimeError, "lock timeout"):
                self.migration.downgrade()
        bind.execute.assert_called_once()
        self.assertEqual(str(bind.execute.call_args.args[0]), "LOCK TABLE submission_flag_reveal_audits IN ACCESS EXCLUSIVE MODE")
        drop.assert_not_called()
        bind.commit.assert_not_called()


class SubmissionAuditTimeHttpTests(unittest.IsolatedAsyncioTestCase):
    def application(self):
        app = FastAPI()
        app.include_router(submissions.flag_logs_router)
        app.include_router(challenges.router)
        result = SimpleNamespace(scalar=lambda: 0, all=lambda: [])
        db = SimpleNamespace(execute=AsyncMock(return_value=result))
        app.dependency_overrides[submissions.get_db] = lambda: db
        return app, db

    async def request(self, path, params):
        app, db = self.application()
        with patch.object(deps, "_decode_bearer_token", return_value={"sub": str(uuid4()), "type": "access"}), patch.object(
            deps, "_require_current_authorization", new=AsyncMock(return_value="admin")
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://owned.invalid") as client:
                response = await client.get(path, params=params)
        return response, db

    async def test_both_queries_reject_epoch_date_only_and_naive_before_database_read(self):
        values = ("123", "+123", "-123", "123.5", "+123.5", "-123.5", "1700000000", "2026-10-18", "00:00:00Z", "2026-10-18T00:00:00", "")
        for path in ("/submissions/logs", "/admin/challenges/solve-metrics"):
            for field in ("submitted_from", "submitted_to"):
                for value in values:
                    with self.subTest(path=path, field=field, value=value):
                        response, db = await self.request(path, {field: value})
                        self.assertEqual(response.status_code, 422)
                        db.execute.assert_not_awaited()

    async def test_both_queries_accept_explicit_utc_offsets_and_equal_inclusive_bounds(self):
        moment = datetime(2026, 10, 18, tzinfo=timezone.utc)
        for path in ("/submissions/logs", "/admin/challenges/solve-metrics"):
            for value in ("2026-10-18T00:00:00Z", "2026-10-18T00:00:00+00:00", "2026-10-18T09:00:00+09:00"):
                with self.subTest(path=path, value=value):
                    response, db = await self.request(path, {"submitted_from": value, "submitted_to": value})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["items"], [])
                    self.assertEqual(db.execute.await_count, 2)
                    for call in db.execute.call_args_list:
                        times = [item for item in call.args[0].compile().params.values() if isinstance(item, datetime)]
                        self.assertEqual(times, [moment, moment])
                        self.assertTrue(all(item.tzinfo is timezone.utc for item in times))

    async def test_both_queries_reject_reversed_explicit_iso_range(self):
        for path in ("/submissions/logs", "/admin/challenges/solve-metrics"):
            response, db = await self.request(path, {"submitted_from": "2026-10-18T00:00:01Z", "submitted_to": "2026-10-18T00:00:00Z"})
            self.assertEqual(response.status_code, 422)
            db.execute.assert_not_awaited()

    def test_query_openapi_remains_typed_datetime(self):
        app, _db = self.application()
        for path in ("/submissions/logs", "/admin/challenges/solve-metrics"):
            parameters = app.openapi()["paths"][path]["get"]["parameters"]
            for name in ("submitted_from", "submitted_to"):
                schema = next(item["schema"] for item in parameters if item["name"] == name)
                self.assertIn({"type": "string", "format": "date-time"}, schema.get("anyOf", [schema]))


if __name__ == "__main__":
    unittest.main()
