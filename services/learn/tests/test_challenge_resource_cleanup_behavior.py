"""삭제 API·원장·Env 응답 계약의 격리 단위 테스트. 실제 DB/HTTP 인수는 별도다."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hmac
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
import uuid

from fastapi import Depends, HTTPException, Query, status
import httpx
from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String,
    UniqueConstraint, and_, delete, func, or_, select,
)
from sqlalchemy.dialects.postgresql import UUID as SQLUUID
from sqlalchemy.orm import declarative_base, relationship, selectinload


ROOT = Path(__file__).resolve().parents[1]
Base = declarative_base()


class Challenge(Base):
    __tablename__ = "fixture_challenges"
    id = Column(Integer, primary_key=True)
    docker_image = Column(String)
    hints = relationship("Hint")


class Hint(Base):
    __tablename__ = "fixture_hints"
    id = Column(Integer, primary_key=True)
    challenge_id = Column(ForeignKey("fixture_challenges.id"))


class HintUnlock(Base):
    __tablename__ = "fixture_hint_unlocks"
    id = Column(Integer, primary_key=True)
    hint_id = Column(Integer)


class Submission(Base):
    __tablename__ = "fixture_submissions"
    id = Column(Integer, primary_key=True)
    challenge_id = Column(Integer)


class ChallengePrerequisite(Base):
    __tablename__ = "fixture_challenge_prerequisites"
    challenge_id = Column(Integer, primary_key=True)
    prerequisite_id = Column(Integer, primary_key=True)


class BuildContextStorageUnavailableError(RuntimeError):
    pass


def load_definitions(path, namespace, names=None):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    statements = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.If)):
            continue
        name = getattr(node, "name", None)
        if isinstance(node, ast.Assign):
            name = getattr(node.targets[0], "id", None)
        if names is not None and name not in names:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node.decorator_list = [] if name != "CleanupSettings" else node.decorator_list
        statements.append(node)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *statements], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / path), "exec"), namespace)


MODEL_NAMESPACE = dict(globals(), UUID=SQLUUID)
load_definitions("app/models/challenge_image_cleanup.py", MODEL_NAMESPACE)
ChallengeImageCleanupJob = MODEL_NAMESPACE["ChallengeImageCleanupJob"]


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value


class Session:
    def __init__(self, *results):
        self.execute = AsyncMock(side_effect=[Result(value) for value in results])
        self.add = Mock()
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def make_job(**values):
    data = dict(id=uuid.uuid4(), challenge_id=23, image_reference="ctf-challenge-23:latest",
                status="pending", attempt_count=0, last_error_code=None,
                available_at=datetime.now(timezone.utc), lease_until=None, completed_at=None)
    data.update(values)
    return ChallengeImageCleanupJob(**data)


def api_namespace():
    namespace = dict(globals(), UUID=uuid.UUID, uuid4=uuid.uuid4, sa_delete=delete,
                     get_db=lambda: None, require_role=lambda *_args: lambda: None,
                     delete_all_build_files=Mock(),
                     _build_context_storage_unavailable=lambda: HTTPException(503, "저장소 정리 실패"))
    load_definitions("app/api/challenges.py", namespace, {
        "_CLEANUP_PUBLIC_ERROR_CODES", "_challenge_cleanup_status", "_latest_challenge_cleanup",
        "_deleted_challenge_cleanup", "get_challenge_cleanup", "retry_challenge_cleanup",
        "list_challenge_cleanup_jobs", "delete_challenge",
    })
    return namespace


def internal_namespace():
    namespace = dict(globals(), UUID=uuid.UUID, get_db=lambda: None,
                     settings=SimpleNamespace(internal_api_key="fixture-internal-key"))
    load_definitions("app/api/internal.py", namespace, {"verify_cleanup_internal_key", "challenge_cleanup_target"})
    return namespace


def worker_namespace():
    namespace = dict(globals(), UUID=uuid.UUID,
                     utc_now=lambda: datetime.now(timezone.utc),
                     settings=SimpleNamespace(env_orchestrator_url="http://env.invalid", internal_api_key="fixture-internal-key"))
    load_definitions("app/workers/challenge_image_cleanup_runner.py", namespace)
    return namespace


class ChallengeResourceCleanupApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_delete_enqueues_owned_or_no_image_in_same_commit(self):
        for image in ("ctf-challenge-23:latest", "external/shared:latest", None):
            with self.subTest(image=image):
                namespace = api_namespace()
                db = Session(Challenge(id=23, docker_image=image, hints=[]), None, None, None, None)
                response = await namespace["delete_challenge"](23, db, "admin")
                job = db.add.call_args.args[0]
                self.assertEqual(job.challenge_id, 23)
                self.assertEqual(job.image_reference, image if image == "ctf-challenge-23:latest" else "")
                self.assertEqual(response["cleanup_job_id"], str(job.id))
                self.assertEqual(response["cleanup_status"], "pending")
                self.assertEqual(response["image_cleanup_pending"], image == "ctf-challenge-23:latest")
                self.assertIn("FOR UPDATE", str(db.execute.call_args_list[0].args[0]))
                db.commit.assert_awaited_once()
                namespace["delete_all_build_files"].assert_called_once_with(23)

    async def test_storage_failure_never_commits_delete_or_outbox(self):
        namespace = api_namespace()
        namespace["delete_all_build_files"].side_effect = BuildContextStorageUnavailableError("private endpoint")
        db = Session(Challenge(id=23, docker_image="ctf-challenge-23:latest", hints=[]), None, None, None)
        with self.assertRaises(HTTPException) as error:
            await namespace["delete_challenge"](23, db, "admin")
        self.assertEqual(error.exception.status_code, 503)
        db.commit.assert_not_awaited()
        self.assertEqual(db.execute.await_count, 4)

    async def test_restored_problem_delete_reactivates_exact_pair_without_losing_history(self):
        for image in ("ctf-challenge-23:latest", "external/shared:latest"):
            with self.subTest(image=image):
                namespace = api_namespace()
                previous_time = datetime.now(timezone.utc) - timedelta(days=3)
                reference = image if image == "ctf-challenge-23:latest" else ""
                job = make_job(status="completed", image_reference=reference, attempt_count=21,
                               completed_at=previous_time, created_at=previous_time, updated_at=previous_time,
                               last_error_code="env_orchestrator_unavailable")
                db = Session(Challenge(id=23, docker_image=image, hints=[]), job, None, None, None)
                result = await namespace["delete_challenge"](23, db, "admin")
                self.assertEqual(result["cleanup_job_id"], str(job.id))
                self.assertEqual(result["cleanup_status"], "pending")
                self.assertEqual(job.attempt_count, 21)
                self.assertEqual(job.created_at, previous_time)
                self.assertEqual(job.completed_at, previous_time)
                self.assertEqual(job.last_error_code, "env_orchestrator_unavailable")
                self.assertIsNone(job.lease_until)
                self.assertGreater(job.available_at, previous_time)
                self.assertEqual(job.updated_at, job.available_at)
                query = db.execute.call_args_list[1].args[0]
                self.assertIn("FOR UPDATE", str(query))
                self.assertEqual(set(query.compile().params.values()), {23, reference})
                db.add.assert_not_called()
                db.commit.assert_awaited_once()

    async def test_repeat_delete_returns_original_job_without_storage_or_new_job(self):
        namespace = api_namespace()
        job = make_job(status="failed", attempt_count=20)
        db = Session(None, job)
        result = await namespace["delete_challenge"](23, db, "admin")
        self.assertEqual(result["cleanup_job_id"], str(job.id))
        self.assertEqual(result["cleanup_status"], "failed")
        db.add.assert_not_called()
        db.commit.assert_not_awaited()
        namespace["delete_all_build_files"].assert_not_called()

    async def test_missing_delete_without_outbox_remains_not_found(self):
        with self.assertRaises(HTTPException) as error:
            await api_namespace()["delete_challenge"](23, Session(None, None), "admin")
        self.assertEqual(error.exception.status_code, 404)

    async def test_status_and_retry_never_target_current_private_or_public_problem(self):
        for endpoint in ("get_challenge_cleanup", "retry_challenge_cleanup"):
            with self.subTest(endpoint=endpoint):
                db = Session(23)
                with self.assertRaises(HTTPException) as error:
                    await api_namespace()[endpoint](23, db, "admin")
                self.assertEqual(error.exception.status_code, 409)
                self.assertEqual(error.exception.detail["code"], "cleanup_challenge_exists")
                self.assertEqual(db.execute.await_count, 1)
                db.commit.assert_not_awaited()

    async def test_manual_retry_keeps_attempt_count_and_completed_job_is_idempotent(self):
        for state in ("failed", "pending", "completed"):
            with self.subTest(state=state):
                job = make_job(status=state, attempt_count=20)
                db = Session(None, job)
                result = await api_namespace()["retry_challenge_cleanup"](23, db, "admin")
                self.assertEqual(result["status"], "completed" if state == "completed" else "pending")
                self.assertEqual(result["attempt_count"], 20)
                self.assertIs(result["retry_budget_reset"], False)
                self.assertEqual(db.commit.await_count, 0 if state == "completed" else 1)

    async def test_processing_retry_does_not_override_claim(self):
        db = Session(None, make_job(status="processing", attempt_count=2,
                                   lease_until=datetime.now(timezone.utc) + timedelta(minutes=1)))
        with self.assertRaises(HTTPException) as error:
            await api_namespace()["retry_challenge_cleanup"](23, db, "admin")
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail["code"], "cleanup_in_progress")
        db.commit.assert_not_awaited()

    async def test_processing_retry_recovers_expired_or_missing_lease_without_resetting_attempt(self):
        for lease in (None, datetime.now(timezone.utc) - timedelta(seconds=1)):
            with self.subTest(lease=lease):
                job = make_job(status="processing", attempt_count=20, lease_until=lease)
                db = Session(None, job)
                result = await api_namespace()["retry_challenge_cleanup"](23, db, "admin")
                self.assertEqual(result["status"], "pending")
                self.assertEqual(result["attempt_count"], 20)
                self.assertIs(result["retry_budget_reset"], False)
                self.assertIsNone(job.lease_until)
                self.assertEqual(job.updated_at, job.available_at)
                db.commit.assert_awaited_once()

    async def test_retry_can_target_older_failed_job_without_retrying_newer_completed_pair(self):
        old = make_job(status="failed", attempt_count=20,
                       updated_at=datetime.now(timezone.utc) - timedelta(days=1))
        newer = make_job(status="completed", image_reference="",
                         updated_at=datetime.now(timezone.utc))
        db = Session()

        async def execute(query):
            if query.column_descriptions[0]["entity"] is Challenge:
                return Result(None)
            params = query.compile().params
            if "id_1" in params:
                self.assertEqual(params["challenge_id_1"], 23)
                return Result(next((job for job in (old, newer) if job.id == params["id_1"]), None))
            return Result(newer)

        db.execute.side_effect = execute
        result = await api_namespace()["retry_challenge_cleanup"](23, db, "admin", job_id=old.id)
        self.assertEqual(result["job_id"], str(old.id))
        self.assertEqual(old.status, "pending")
        self.assertEqual(old.attempt_count, 20)
        self.assertEqual(newer.status, "completed")
        self.assertIn("FOR UPDATE", str(db.execute.call_args.args[0]))
        db.commit.assert_awaited_once()

    async def test_retry_unknown_or_other_challenge_job_is_not_found_without_latest_fallback(self):
        job_id = uuid.uuid4()
        db = Session(None, None)
        with self.assertRaises(HTTPException) as error:
            await api_namespace()["retry_challenge_cleanup"](23, db, "admin", job_id=job_id)
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(set(db.execute.call_args.args[0].compile().params.values()), {job_id, 23})
        self.assertEqual(db.execute.await_count, 2)
        db.commit.assert_not_awaited()

    async def test_list_excludes_current_problems_completed_and_caps_rows(self):
        db = Session(1, [make_job(status="failed", last_error_code="private response")])
        result = await api_namespace()["list_challenge_cleanup_jobs"](None, db, "admin")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["last_error_code"], "challenge_image_cleanup_failed")
        for call in db.execute.call_args_list:
            query = call.args[0]
            self.assertIn("NOT (EXISTS", str(query))
            self.assertEqual(query.compile().params["status_1"], ["pending", "processing", "failed"])
        self.assertEqual(db.execute.call_args.args[0]._limit_clause.value, 100)
        self.assertIn("updated_at DESC", str(db.execute.call_args.args[0]._order_by_clauses[0]))

    async def test_proof_requires_deleted_row_matching_job_and_owned_or_empty_image(self):
        namespace = internal_namespace()
        for image in ("ctf-challenge-23:latest", ""):
            for state in ("pending", "processing", "failed", "completed"):
                job = make_job(image_reference=image, status=state)
                db = Session(None, job)
                result = await namespace["challenge_cleanup_target"](23, job.id, db)
                self.assertEqual(result, dict(job_id=str(job.id), challenge_id=23, deleted=True, image_reference=image))
                self.assertEqual(set(db.execute.call_args.args[0].compile().params.values()), {job.id, 23})
        for results, expected in (((23,), 409), ((None, None), 404), ((None, make_job(image_reference="external/shared:latest")), 409)):
            with self.subTest(expected=expected):
                with self.assertRaises(HTTPException) as error:
                    await namespace["challenge_cleanup_target"](23, uuid.uuid4(), Session(*results))
                self.assertEqual(error.exception.status_code, expected)

    def test_proof_key_has_no_development_bypass_and_admin_routes_remain_admin_only(self):
        namespace = internal_namespace()
        request = SimpleNamespace(headers={"X-Internal-Key": "fixture-internal-key"})
        namespace["verify_cleanup_internal_key"](request)
        for key, header, expected in (("", "", 503), ("fixture-internal-key", "wrong", 403)):
            namespace["settings"].internal_api_key = key
            with self.assertRaises(HTTPException) as error:
                namespace["verify_cleanup_internal_key"](SimpleNamespace(headers={"X-Internal-Key": header}))
            self.assertEqual(error.exception.status_code, expected)
        api_tree = ast.parse((ROOT / "app/api/challenges.py").read_text(encoding="utf-8"))
        for endpoint in ("delete_challenge", "get_challenge_cleanup", "retry_challenge_cleanup", "list_challenge_cleanup_jobs"):
            node = next(item for item in api_tree.body if getattr(item, "name", None) == endpoint)
            self.assertIn('require_role(\'admin\')', ast.unparse(node.args))
        internal_tree = ast.parse((ROOT / "app/api/internal.py").read_text(encoding="utf-8"))
        endpoint = next(item for item in internal_tree.body if getattr(item, "name", None) == "challenge_cleanup_target")
        self.assertIn("Depends(verify_cleanup_internal_key)", ast.unparse(endpoint.decorator_list[0]))


class ChallengeResourceCleanupWorkerTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, **settings):
        namespace = worker_namespace()
        worker = namespace["ChallengeImageCleanupRunner"](namespace["CleanupSettings"](**settings))
        return namespace, worker

    async def request(self, payload, status_code=200):
        namespace, worker = self.worker()
        job = make_job()
        actual_client = httpx.AsyncClient
        requests = []

        async def respond(request):
            requests.append(request)
            return httpx.Response(status_code, json=payload(job) if callable(payload) else payload)

        namespace["httpx"] = SimpleNamespace(
            AsyncClient=lambda **kwargs: actual_client(transport=httpx.MockTransport(respond), **kwargs),
            Timeout=httpx.Timeout, RequestError=httpx.RequestError,
        )
        result = await worker._request_cleanup(job.id, 23, job.image_reference)
        return result, requests

    async def test_request_is_bound_to_job_and_full_response_target(self):
        for state in ("removed", "absent"):
            result, requests = await self.request(lambda job: dict(challenge_id=23, job_id=str(job.id), status=state, image_reference=job.image_reference))
            self.assertEqual(result, state)
            self.assertEqual(requests[0].url.path, "/internal/challenges/23/cleanup-resources")
            self.assertEqual(set(json.loads(requests[0].content)), {"job_id"})
            self.assertEqual(requests[0].headers["X-Internal-Key"], "fixture-internal-key")

    async def test_invalid_or_other_job_target_never_completes(self):
        mutations = (
            {"job_id": str(uuid.uuid4())}, {"challenge_id": 24}, {"challenge_id": True},
            {"image_reference": "external/shared:latest"}, {"status": "pending"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    await self.request(lambda job: dict(challenge_id=23, job_id=str(job.id), status="removed", image_reference=job.image_reference) | mutation)
        with self.assertRaises(ValueError):
            await self.request([])

    async def test_boundary_errors_are_safe_and_current_problem_is_distinct(self):
        for status_code, payload, expected in (
            (409, {"detail": {"code": "cleanup_challenge_exists", "message": "private upstream"}}, "cleanup_challenge_exists"),
            (409, {"code": "other", "error": "private upstream"}, "cleanup_target_rejected"),
            (503, {"error": "private upstream"}, "env_orchestrator_unavailable"),
        ):
            with self.subTest(status_code=status_code):
                try:
                    await self.request(payload, status_code)
                except RuntimeError as error:
                    self.assertEqual(str(error), expected)
                else:
                    self.fail("failure response was accepted")

    async def test_no_image_contract_and_cancel_preserve_claim_for_lease_recovery(self):
        namespace, worker = self.worker()
        job = make_job(image_reference="")
        worker._claim_one = AsyncMock(return_value=(job.id, 23, "", 1))
        worker._request_cleanup = AsyncMock(side_effect=asyncio.CancelledError())
        worker._finish_failure = AsyncMock()
        worker._finish_success = AsyncMock()
        with self.assertRaises(asyncio.CancelledError):
            await worker.run_once()
        worker._request_cleanup.assert_awaited_once_with(job.id, 23, "")
        worker._finish_failure.assert_not_awaited()
        worker._finish_success.assert_not_awaited()
        self.assertFalse(namespace["_validate_image_reference"](23, "external/shared:latest"))

    async def test_run_once_forwards_job_and_records_only_safe_owned_attempt_result(self):
        for mode in ("success", "unavailable", "foreign_image", "stale"):
            with self.subTest(mode=mode):
                _namespace, worker = self.worker()
                job = make_job(image_reference="external/shared:latest" if mode == "foreign_image" else "")
                worker._claim_one = AsyncMock(return_value=(job.id, 23, job.image_reference, 3))
                worker._request_cleanup = AsyncMock(return_value="absent")
                worker._finish_success = AsyncMock(return_value=mode != "stale")
                worker._finish_failure = AsyncMock(return_value=True)
                if mode == "unavailable":
                    worker._request_cleanup.side_effect = httpx.ConnectError("private upstream address")
                result = await worker.run_once()
                self.assertEqual(result, {"success": "completed", "unavailable": "failed", "foreign_image": "failed", "stale": "stale"}[mode])
                if mode in {"success", "stale"}:
                    worker._request_cleanup.assert_awaited_once_with(job.id, 23, "")
                    worker._finish_success.assert_awaited_once_with(job.id, 3)
                    worker._finish_failure.assert_not_awaited()
                else:
                    worker._finish_success.assert_not_awaited()
                    worker._finish_failure.assert_awaited_once_with(
                        job.id, 3, "env_orchestrator_unavailable" if mode == "unavailable" else "invalid_cleanup_response",
                    )
                if mode == "foreign_image":
                    worker._request_cleanup.assert_not_awaited()

    async def test_claim_uses_locked_skip_locked_and_increments_attempt(self):
        namespace, worker = self.worker(lease_seconds=30)
        job = make_job(attempt_count=3)
        db = Session(job)
        namespace["async_session"] = lambda: db
        result = await worker._claim_one()
        self.assertEqual(result, (job.id, 23, job.image_reference, 4))
        self.assertEqual(job.status, "processing")
        self.assertIsNotNone(job.lease_until)
        self.assertTrue(db.execute.call_args.args[0]._for_update_arg.skip_locked)
        db.commit.assert_awaited_once()

    async def test_late_success_and_failure_cannot_overwrite_new_claim(self):
        for method in ("_finish_success", "_finish_failure"):
            with self.subTest(method=method):
                namespace, worker = self.worker()
                db = Session(None)
                namespace["async_session"] = lambda: db
                job_id = uuid.uuid4()
                args = (job_id, 2) if method == "_finish_success" else (job_id, 2, "env_orchestrator_unavailable")
                self.assertIs(await getattr(worker, method)(*args), False)
                params = db.execute.call_args.args[0].compile().params
                self.assertEqual(set(params.values()), {job_id, 2, "processing"})
                db.commit.assert_not_awaited()
                db.rollback.assert_awaited_once()

    async def test_failure_budget_backoff_and_success_are_retained(self):
        for attempt, expected in ((1, "pending"), (20, "failed"), (21, "failed")):
            namespace, worker = self.worker(max_attempts=20)
            job = make_job(status="processing", attempt_count=attempt)
            db = Session(job)
            namespace["async_session"] = lambda: db
            self.assertIs(await worker._finish_failure(job.id, attempt, "env_orchestrator_unavailable"), True)
            self.assertEqual(job.status, expected)
            self.assertEqual(job.attempt_count, attempt)
            self.assertIsNone(job.lease_until)
        namespace, worker = self.worker()
        job = make_job(status="processing", attempt_count=21, last_error_code="env_orchestrator_unavailable")
        db = Session(job)
        namespace["async_session"] = lambda: db
        self.assertIs(await worker._finish_success(job.id, 21), True)
        self.assertEqual(job.status, "completed")
        self.assertIsNone(job.last_error_code)
        self.assertIsNotNone(job.completed_at)


if __name__ == "__main__":
    unittest.main()
