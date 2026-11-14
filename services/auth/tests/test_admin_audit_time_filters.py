"""감사 시각 필터의 HTTP/UTC/공통 SQL 조건을 DB 없이 검증한다."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.api import admin, deps  # noqa: E402


def session(*, total=0, rows=()):
    return SimpleNamespace(
        execute=AsyncMock(side_effect=[
            SimpleNamespace(scalar=lambda: total),
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(rows))),
        ]),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )


async def invoke(db, **overrides):
    values = {
        "user": SimpleNamespace(id=uuid4(), role="admin"),
        "action": "", "action_prefix": "", "search": "",
        "created_from": None, "created_to": None, "page": 1, "size": 50,
    }
    values.update(overrides)
    return await admin.admin_audit_logs(db=db, **values)


def compiled(statement):
    return statement.compile(dialect=postgresql.dialect())


class AdminAuditTimeFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_offset_bounds_normalize_to_utc_and_apply_to_same_count_and_rows_query(self):
        start = datetime(2026, 10, 18, 9, tzinfo=timezone(timedelta(hours=9)))
        end = start + timedelta(hours=1)
        db = session()
        result = await invoke(db, created_from=start, created_to=end, action="auth.login", search="owned%_\\actor", page=2, size=10)
        count_query, rows_query = [call.args[0] for call in db.execute.call_args_list]
        count_source = count_query.get_final_froms()[0].element
        self.assertEqual(count_source._where_criteria, rows_query._where_criteria)
        query = compiled(rows_query)
        self.assertEqual(query.params["created_at_1"], start.astimezone(timezone.utc))
        self.assertEqual(query.params["created_at_2"], end.astimezone(timezone.utc))
        self.assertIs(query.params["created_at_1"].tzinfo, timezone.utc)
        self.assertIn("audit_logs.created_at >=", str(query))
        self.assertIn("audit_logs.created_at <=", str(query))
        self.assertIn("audit_logs.action =", str(query))
        self.assertIn("ILIKE", str(query))
        self.assertIn("%owned\\%\\_\\\\actor%", query.params.values())
        self.assertNotIn("owned%_\\actor", str(query))
        self.assertIn("ORDER BY audit_logs.created_at DESC, audit_logs.id DESC", str(query))
        self.assertEqual(rows_query._offset_clause.value, 10)
        self.assertEqual(rows_query._limit_clause.value, 10)
        self.assertEqual(result, {"total": 0, "page": 2, "size": 10, "items": []})
        db.commit.assert_not_awaited()
        db.rollback.assert_not_awaited()

    async def test_equal_bounds_are_valid_and_inclusive(self):
        moment = datetime(2026, 10, 18, 1, tzinfo=timezone.utc)
        db = session()
        await invoke(db, created_from=moment, created_to=moment)
        query = compiled(db.execute.call_args_list[1].args[0])
        self.assertEqual(query.params["created_at_1"], query.params["created_at_2"])
        self.assertIn("created_at >=", str(query))
        self.assertIn("created_at <=", str(query))

    async def test_one_sided_or_absent_bounds_preserve_existing_filters(self):
        moment = datetime(2026, 10, 18, 1, tzinfo=timezone.utc)
        for start, end, expected in ((moment, None, ">="), (None, moment, "<="), (None, None, None)):
            with self.subTest(start=start, end=end):
                db = session()
                await invoke(db, created_from=start, created_to=end, action_prefix="settings.")
                query = compiled(db.execute.call_args_list[1].args[0])
                self.assertIn("audit_logs.action LIKE", str(query))
                self.assertIn("settings.%", query.params.values())
                if expected:
                    self.assertIn(f"audit_logs.created_at {expected}", str(query))
                else:
                    self.assertNotIn("created_at_1", query.params)

    async def test_naive_and_reversed_bounds_are_422_before_database_read(self):
        aware = datetime(2026, 10, 18, tzinfo=timezone.utc)
        naive = aware.replace(tzinfo=None)
        for bounds in (
            {"created_from": naive}, {"created_to": naive},
            {"created_from": aware + timedelta(seconds=1), "created_to": aware},
        ):
            with self.subTest(bounds=bounds):
                db = session()
                with self.assertRaises(HTTPException) as caught:
                    await invoke(db, **bounds)
                self.assertEqual(caught.exception.status_code, 422)
                db.execute.assert_not_awaited()
                db.commit.assert_not_awaited()

    async def test_existing_response_fields_and_total_are_preserved(self):
        row = SimpleNamespace(
            id=uuid4(), actor_id=None, action="auth.login", target_type=None,
            target_id=None, details={"owned": True}, ip_address="127.0.0.1",
            created_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
        )
        db = session(total=1, rows=[row])
        result = await invoke(db, created_from=row.created_at, created_to=row.created_at)
        self.assertEqual(result, {"total": 1, "page": 1, "size": 50, "items": [{
            "id": str(row.id), "actor": "system", "action": "auth.login", "target": "",
            "details": {"owned": True}, "ip_address": "127.0.0.1", "created_at": row.created_at.isoformat(),
        }]})

    async def request(self, *, role="admin", params=None, fail=False):
        app = FastAPI()
        app.include_router(admin.router, prefix="/admin")
        db = session()
        if fail:
            db.execute.side_effect = RuntimeError("synthetic read failure")
        app.dependency_overrides[admin.get_db] = lambda: db

        async def current_user():
            if role is None:
                raise HTTPException(status_code=401, detail="인증이 필요합니다.")
            return SimpleNamespace(id=uuid4(), role=role)

        app.dependency_overrides[deps.get_current_user] = current_user
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://owned.invalid") as client:
            response = await client.get("/admin/audit-logs", params=params)
        return response, db

    async def test_http_admin_and_moderator_read_but_student_and_anonymous_are_denied(self):
        for role, expected in (("admin", 200), ("moderator", 200), ("student", 403), (None, 401)):
            with self.subTest(role=role):
                response, db = await self.request(role=role, params={"created_from": "2026-10-18T00:00:00Z", "created_to": "2026-10-18T01:00:00Z"})
                self.assertEqual(response.status_code, expected)
                if expected != 200:
                    db.execute.assert_not_awaited()
                db.commit.assert_not_awaited()

    async def test_http_invalid_format_timezone_range_and_page_are_rejected(self):
        for params in (
            {"created_from": "not-a-date"}, {"created_from": "2026-10-18T00:00:00"},
            {"created_to": "2026-10-18T00:00:00"},
            {"created_from": "2026-10-18T01:00:00Z", "created_to": "2026-10-18T00:00:00Z"},
            {"page": 0},
        ):
            with self.subTest(params=params):
                response, db = await self.request(params=params)
                self.assertEqual(response.status_code, 422)
                db.execute.assert_not_awaited()

    async def test_http_epoch_numeric_and_date_only_cannot_bypass_explicit_iso_timezone(self):
        for field in ("created_from", "created_to"):
            for value in ("123", "+123", "-123", "123.5", "+123.5", "-123.5", "1700000000", "2026-10-18", "00:00:00Z", ""):
                with self.subTest(field=field, value=value):
                    response, db = await self.request(params={field: value})
                    self.assertEqual(response.status_code, 422)
                    db.execute.assert_not_awaited()

    async def test_http_explicit_utc_offset_and_equal_bounds_remain_valid(self):
        moment = datetime(2026, 10, 18, tzinfo=timezone.utc)
        for value in ("2026-10-18T00:00:00Z", "2026-10-18T00:00:00+00:00", "2026-10-18T09:00:00+09:00"):
            with self.subTest(value=value):
                response, db = await self.request(params={"created_from": value, "created_to": value})
                self.assertEqual(response.status_code, 200)
                query = compiled(db.execute.call_args_list[1].args[0])
                self.assertEqual(query.params["created_at_1"], moment)
                self.assertEqual(query.params["created_at_2"], moment)
                self.assertIs(query.params["created_at_1"].tzinfo, timezone.utc)
                self.assertIn("created_at >=", str(query))
                self.assertIn("created_at <=", str(query))

    def test_http_query_schema_remains_datetime_not_untyped_string(self):
        app = FastAPI()
        app.include_router(admin.router, prefix="/admin")
        parameters = app.openapi()["paths"]["/admin/audit-logs"]["get"]["parameters"]
        for name in ("created_from", "created_to"):
            schema = next(item["schema"] for item in parameters if item["name"] == name)
            self.assertIn({"type": "string", "format": "date-time"}, schema.get("anyOf", [schema]))

    async def test_read_failure_is_not_an_empty_success_and_a_new_read_can_recover(self):
        failed, failed_db = await self.request(fail=True)
        self.assertEqual(failed.status_code, 500)
        self.assertNotIn('"items"', failed.text)
        recovered, db = await self.request()
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json()["items"], [])
        failed_db.commit.assert_not_awaited()
        db.commit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
