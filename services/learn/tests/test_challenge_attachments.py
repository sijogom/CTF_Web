"""AP1 계약 회귀. 제품 구현 release 후 실행하며 실제 PG/MinIO 검증은 별도다."""

from __future__ import annotations

import ast
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4, uuid5, NAMESPACE_URL


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))


def source_function(relative: str, name: str):
    text = (SERVICE_ROOT / relative).read_text(encoding="utf-8")
    tree = ast.parse(text)
    return next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)


def immediate(coroutine):
    """외부 I/O를 mock한 실제 callback을 socket/event-loop 생성 없이 완료한다."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError("pure callback unexpectedly suspended")


def rows_result(rows):
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def memory_upload(name="x.bin", data=b"abc"):
    from fastapi import UploadFile
    stream = io.BytesIO(data)
    stream._rolled = False
    return UploadFile(stream, filename=name)


class AttachmentSchemaTests(unittest.TestCase):
    def test_editor_ids_require_draft_but_import_same_parent_projection_does_not(self):
        from fastapi import HTTPException
        from app.schemas.challenge import ChallengeUpdate
        from app.services.challenge_attachments import require_edit_draft
        from app.services.challenge_imports import prepare_import
        for ids in ([], [uuid4()]):
            with self.subTest(ids=ids), self.assertRaises(HTTPException) as caught:
                require_edit_draft(ChallengeUpdate(attachment_ids=ids))
            self.assertEqual(caught.exception.status_code, 422)
            self.assertEqual(caught.exception.detail["code"], "attachment_draft_required")
        require_edit_draft(ChallengeUpdate(files=["legacy.bin"]))
        require_edit_draft(ChallengeUpdate(attachment_ids=[], attachment_draft_id=uuid4()))
        _, records = prepare_import({"version": 2, "challenges": [{"id": 17, "name": "same", "attachment_ids": []}]})
        self.assertEqual(records[0].attachment_ids, ())

    def test_draft_is_uuid_owned_and_target_is_positive_or_new(self):
        from pydantic import ValidationError
        from app.schemas.challenge_attachment import AttachmentDraftRequest
        draft = uuid4()
        self.assertEqual(AttachmentDraftRequest(draft_id=draft).draft_id, draft)
        self.assertEqual(AttachmentDraftRequest(draft_id=draft, challenge_id=17).challenge_id, 17)
        with self.assertRaises(ValidationError):
            AttachmentDraftRequest(draft_id=uuid5(NAMESPACE_URL, "not-client-v4"))
        for value in (0, -1, True, "17"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                AttachmentDraftRequest(draft_id=draft, challenge_id=value)

    def test_create_update_omit_and_explicit_empty_have_distinct_meaning(self):
        from pydantic import ValidationError
        from app.schemas.challenge import ChallengeCreate, ChallengeUpdate
        identifier, draft = uuid4(), uuid4()
        self.assertNotIn("attachment_ids", ChallengeUpdate(name="kept").model_dump(exclude_unset=True))
        self.assertEqual(ChallengeUpdate(attachment_ids=[]).model_dump(exclude_unset=True)["attachment_ids"], [])
        values = ChallengeCreate(name="AP1", flag_value="AP1", attachment_draft_id=draft, attachment_ids=[identifier]).model_dump()
        self.assertEqual(values["attachment_ids"], [identifier])
        for value in (None, [identifier, identifier], ["arbitrary-storage-path"], "not-a-list"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ChallengeUpdate(attachment_ids=value)

    def test_upload_response_never_exposes_object_path_or_presigned_authority(self):
        from pydantic import ValidationError
        from app.schemas.challenge_attachment import AttachmentUploadResponse
        values = dict(id=uuid4(), draft_id=uuid4(), original_name="자료 %.pcap", byte_size=3, sha256="a" * 64,
                      content_type="application/octet-stream", state="ready", ready_for_attach=True,
                      reconcile_required=False, retry_allowed=False)
        result = AttachmentUploadResponse(**values).model_dump()
        self.assertEqual(result["original_name"], "자료 %.pcap")
        self.assertNotIn("object_path", result)
        self.assertNotIn("url", result)
        for extra in ({"object_path": "private/other"}, {"url": "https://outside.invalid/"}):
            with self.assertRaises(ValidationError):
                AttachmentUploadResponse(**values, **extra)

    def test_upload_metadata_preserves_existing_zero_and_100mib_boundary(self):
        from pydantic import ValidationError
        from app.schemas.challenge_attachment import AttachmentUploadResponse
        values = dict(id=uuid4(), draft_id=uuid4(), original_name="empty.bin", sha256="a" * 64,
                      content_type="application/octet-stream", state="ready", ready_for_attach=True,
                      reconcile_required=False, retry_allowed=False)
        for size in (0, 100 * 1024 * 1024):
            self.assertEqual(AttachmentUploadResponse(byte_size=size, **values).byte_size, size)
        for size in (-1, 100 * 1024 * 1024 + 1, True):
            with self.subTest(size=size), self.assertRaises(ValidationError):
                AttachmentUploadResponse(byte_size=size, **values)


class AttachmentServiceTests(unittest.TestCase):
    def asset(self, **values):
        owner, draft, identifier = uuid4(), uuid4(), uuid4()
        row = SimpleNamespace(id=identifier, owner_id=owner, draft_id=draft, original_name="sample.bin",
                              byte_size=3, sha256="a" * 64, state="ready", challenge_id=None,
                              bound_challenge_id=None, content_type="application/octet-stream")
        row.__dict__.update(values)
        return row

    def test_upload_identity_replay_matches_exact_original_metadata(self):
        from app.services.challenge_attachments import validate_upload_identity
        from fastapi import HTTPException
        row = self.asset()
        kwargs = dict(owner_id=row.owner_id, draft_id=row.draft_id, upload_id=row.id,
                      original_name=row.original_name, byte_size=row.byte_size, sha256=row.sha256)
        validate_upload_identity(row, **kwargs)
        for change in ({"original_name": "changed.bin"}, {"byte_size": 4}, {"sha256": "b" * 64}):
            with self.subTest(change=change), self.assertRaises(HTTPException) as caught:
                validate_upload_identity(row, **{**kwargs, **change})
            self.assertEqual(caught.exception.status_code, 409)
        for change in ({"owner_id": uuid4()}, {"draft_id": uuid4()}, {"upload_id": uuid4()}):
            with self.subTest(change=change), self.assertRaises(HTTPException) as caught:
                validate_upload_identity(row, **{**kwargs, **change})
            self.assertEqual(caught.exception.status_code, 404)

    def test_attachment_only_revision_cas_and_same_result_idempotence(self):
        from app.services.challenge_attachments import binding_revision
        from fastapi import HTTPException
        first, second = uuid4(), uuid4()
        self.assertEqual(binding_revision([first], [first], base_revision=0, current_revision=3), 3)
        self.assertEqual(binding_revision([first], [first, second], base_revision=3, current_revision=3), 4)
        self.assertEqual(binding_revision([first, second], [second, first], base_revision=3, current_revision=3), 4)
        self.assertEqual(binding_revision([first], [], base_revision=3, current_revision=3), 4)
        with self.assertRaises(HTTPException) as caught:
            binding_revision([first, second], [first], base_revision=2, current_revision=3)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(binding_revision([first], [first], base_revision=None, current_revision=3,
            current_files=["old.bin"], requested_files=["new.bin"]), 4)
        with self.assertRaises(HTTPException):
            binding_revision([first], [first], base_revision=2, current_revision=3,
                current_files=["old.bin"], requested_files=[])
        self.assertEqual(binding_revision([first], [first], base_revision=2, current_revision=3,
            current_files=["old.bin"], requested_files=["old.bin"]), 3)

    def test_status_missing_is_not_unknown_and_active_upload_cannot_be_missing(self):
        from app.services.challenge_attachments import project_upload_status
        row = self.asset(state="uncertain")
        unknown = project_upload_status(row, active=False, observed_state="unknown")
        self.assertEqual((unknown.state, unknown.retry_allowed, unknown.reconcile_required), ("pending", False, True))
        active_missing = project_upload_status(row, active=True, observed_state="missing")
        self.assertEqual((active_missing.state, active_missing.retry_allowed), ("pending", False))
        missing = project_upload_status(row, active=False, observed_state="missing")
        self.assertEqual((missing.state, missing.retry_allowed), ("missing", True))
        verified = project_upload_status(row, active=False, observed_state="verified")
        self.assertEqual((verified.state, verified.ready_for_attach, verified.reconcile_required), ("ready", False, True))
        row.state = "ready"
        ready = project_upload_status(row, active=False, observed_state="verified")
        self.assertEqual((ready.state, ready.ready_for_attach, ready.reconcile_required), ("ready", True, False))

    def test_legacy_parent_resolution_does_not_trust_raw_bucket_or_other_challenge(self):
        from app.services.challenge_attachments import legacy_object_path
        for value in ("challenges/misc/easy/17/abc.bin", "/storage/challenges/misc/easy/17/abc.bin", "/ctf/storage/challenges/misc/easy/17/abc.bin"):
            self.assertEqual(legacy_object_path(value, 17), "challenges/misc/easy/17/abc.bin")
        for value in ("only-a-name.bin", "challenges/misc/easy/18/abc.bin", "education-private-assets/x/y.bin",
                      "challenge-private-assets/x/y.bin", "challenge-pending/misc/easy/17/abc.bin",
                      "https://outside.invalid/challenges/misc/easy/17/abc.bin", "challenges/misc/easy/17/../other.bin"):
            with self.subTest(value=value):
                self.assertIsNone(legacy_object_path(value, 17))

    def test_existing_filename_policy_and_100mib_are_reused_not_lesson_quotas(self):
        from app.services import challenge_attachments as service
        from app.utils.minio_client import MAX_FILE_SIZE, validate_filename
        self.assertEqual(service.MAX_FILE_SIZE, MAX_FILE_SIZE)
        self.assertEqual(MAX_FILE_SIZE, 100 * 1024 * 1024)
        self.assertIs(service.validate_filename, validate_filename)
        for value in ("x.pcap", "x.elf", "자료.bin", "empty.txt"):
            self.assertIsNone(service.validate_filename(value))
        for value in ("../bad.bin", "bad.exe", "bad.ps1", "a\x00b.txt"):
            self.assertIsNotNone(service.validate_filename(value))


class AttachmentIntegrationContractTests(unittest.TestCase):
    def test_bind_helper_does_not_commit_or_delete_storage(self):
        node = source_function("app/services/challenge_attachments.py", "bind_attachments")
        calls = [child for child in ast.walk(node) if isinstance(child, ast.Call)]
        self.assertFalse(any(isinstance(call.func, ast.Attribute) and call.func.attr in {"commit", "delete_object", "remove_object"} for call in calls))
        names = {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}
        self.assertIn("binding_revision", names)
        self.assertIn("actor_id", names)

    def test_upload_and_reconcile_share_physical_pg_lock_not_local_busy_or_ttl(self):
        path = SERVICE_ROOT / "app/services/challenge_attachments.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn("pg_try_advisory_xact_lock", source)
        self.assertNotIn("asyncio.Lock(", source)
        self.assertNotIn("threading.Lock(", source)
        for name in ("upload_attachment", "reconcile_upload", "get_upload_status"):
            node = source_function("app/services/challenge_attachments.py", name)
            calls = [child for child in ast.walk(node) if isinstance(child, ast.Call)]
            self.assertTrue(any(isinstance(call.func, ast.Name) and call.func.id == "try_upload_lock" for call in calls), name)
        status = source_function("app/services/challenge_attachments.py", "get_upload_status")
        self.assertFalse(any(isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "commit" for child in ast.walk(status)))

    def test_exact_routes_and_permission_stream_headers(self):
        source = (SERVICE_ROOT / "app/api/challenge_attachments.py").read_text(encoding="utf-8")
        routes = {(node.func.attr, node.args[0].value) for item in ast.parse(source).body if isinstance(item, ast.AsyncFunctionDef)
                  for node in item.decorator_list if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args and isinstance(node.args[0], ast.Constant)}
        self.assertTrue({("post", "/challenge-attachment-drafts"), ("get", "/challenge-attachment-drafts/{draft_id}"),
                         ("put", "/challenge-attachment-drafts/{draft_id}/uploads/{upload_id}"),
                         ("get", "/challenge-attachment-drafts/{draft_id}/uploads/{upload_id}"),
                         ("post", "/challenge-attachment-drafts/{draft_id}/uploads/{upload_id}/reconcile"),
                         ("get", "/challenges/{challenge_id}/attachments/{asset_id}"),
                         ("get", "/competitions/{competition_id}/challenges/{challenge_id}/attachments/{asset_id}")}.issubset(routes))
        service = (SERVICE_ROOT / "app/services/challenge_attachments.py").read_text(encoding="utf-8")
        for value in ("Content-Disposition", "attachment;", "X-Content-Type-Options", "nosniff", "iter_object_stream", "close_object_stream"):
            self.assertIn(value, source + service)

    def test_parent_create_patch_import_bind_before_the_existing_commit(self):
        for relative, name in (("app/api/challenges.py", "create_challenge"), ("app/api/challenges.py", "update_challenge"),
                               ("app/services/challenge_imports.py", "import_challenge_batch")):
            text = (SERVICE_ROOT / relative).read_text(encoding="utf-8")
            node = source_function(relative, name)
            body = ast.get_source_segment(text, node)
            self.assertIn("bind_attachments(", body)
            self.assertLess(body.index("bind_attachments("), body.index("await db.commit()"))

    def test_student_copy_failure_cannot_be_swallowed_or_remove_pending_before_commit(self):
        text = (SERVICE_ROOT / "app/api/submissions.py").read_text(encoding="utf-8")
        node = source_function("app/api/submissions.py", "approve_submission")
        body = ast.get_source_segment(text, node)
        self.assertIn("prepare_submission_attachments(", body)
        self.assertIn("await db.rollback()", body)
        self.assertLess(body.index("prepare_submission_attachments("), body.index('sub.status = "approved"'))
        self.assertLess(body.index('sub.status = "approved"'), body.index("await db.commit()"))
        self.assertNotIn("move_to_approved(", body)
        self.assertNotIn("delete_pending_files(", body)

    def test_json_import_keeps_legacy_scalar_and_routes_private_refs_through_binding(self):
        from app.services.challenge_imports import prepare_import
        source = (SERVICE_ROOT / "app/services/challenge_imports.py").read_text(encoding="utf-8")
        old = ["only-a-name.bin", "challenges/misc/easy/17/existing.bin"]
        _, records = prepare_import({"version": 2, "challenges": [{"id": 17, "name": "kept", "files": old}]})
        self.assertEqual(records[0].fields["files"], old)
        self.assertNotIn("attachment_ids", records[0].fields)
        self.assertIn("attachment_ids", source)
        self.assertIn("attachment_draft_id", source)
        self.assertIn("bind_attachments(", source)


class AttachmentCallbackTests(unittest.TestCase):
    def db(self, rows=()):
        return SimpleNamespace(execute=AsyncMock(return_value=rows_result(rows)),
            commit=AsyncMock(), rollback=AsyncMock(), flush=AsyncMock(), add=Mock())

    def asset(self, **extra):
        row = SimpleNamespace(id=uuid4(), draft_id=uuid4(), owner_id=uuid4(), original_name="sample.bin",
            byte_size=3, sha256="a" * 64, content_type="application/octet-stream", state="ready",
            challenge_id=None, bound_challenge_id=None, position=0, object_path="owned")
        row.__dict__.update(extra)
        return row

    def test_actual_status_is_nonmutating_and_does_not_stat_when_other_put_has_lock(self):
        from app.services import challenge_attachments as service
        row, db = self.asset(), self.db()
        with patch.object(service, "owned_upload", AsyncMock(return_value=row)), \
             patch.object(service, "try_upload_lock", AsyncMock(return_value=False)), \
             patch.object(service, "observe_upload", AsyncMock()) as observe:
            response = immediate(service.get_upload_status(db, row.draft_id, row.id, row.owner_id))
        self.assertEqual((response.state, response.retry_allowed), ("pending", False))
        observe.assert_not_awaited()
        db.commit.assert_not_awaited()
        db.add.assert_not_called()

    def test_uuid_lock_is_transaction_scoped_and_parameter_bound_on_same_session(self):
        from app.services import challenge_attachments as service
        db = SimpleNamespace(scalar=AsyncMock(return_value=True))
        identifier = uuid4()
        self.assertTrue(immediate(service.try_upload_lock(db, identifier)))
        first = db.scalar.await_args
        self.assertIn("pg_try_advisory_xact_lock(:key)", str(first.args[0]))
        self.assertEqual(set(first.args[1]), {"key"})
        self.assertIs(type(first.args[1]["key"]), int)
        immediate(service.try_upload_lock(db, identifier))
        self.assertEqual(first.args[1], db.scalar.await_args.args[1])

    def test_actual_reconcile_only_verified_object_commits_and_never_puts(self):
        from app.services import challenge_attachments as service
        for observed in ("verified", "missing", "unknown"):
            row, db = self.asset(state="pending"), self.db()
            with self.subTest(observed=observed), patch.object(service, "owned_upload", AsyncMock(return_value=row)), \
                 patch.object(service, "try_upload_lock", AsyncMock(return_value=True)), \
                 patch.object(service, "observe_upload", AsyncMock(return_value=observed)), \
                 patch.object(service, "put_private_challenge_object", Mock()) as put:
                result = immediate(service.reconcile_upload(db, row.draft_id, row.id, row.owner_id))
                self.assertEqual(db.commit.await_count, int(observed == "verified"))
                self.assertEqual(result.ready_for_attach, observed == "verified")
                put.assert_not_called()

    def test_new_upload_metadata_commit_precedes_physical_put_and_ready_commit(self):
        import hashlib
        from fastapi import UploadFile
        from app.services import challenge_attachments as service
        owner, draft, identifier = uuid4(), uuid4(), uuid4()
        events, db = [], self.db()
        db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
        async def commit():
            events.append("commit")
        async def threaded(fn, *args):
            if fn is service.put_private_challenge_object:
                events.append("put")
                self.assertEqual(args[2], 3)
                self.assertEqual(args[1].read(), b"abc")
            return None
        db.commit.side_effect = commit
        with patch.object(service, "owned_draft", AsyncMock(return_value=SimpleNamespace(state="open"))), \
             patch.object(service, "try_upload_lock", AsyncMock(return_value=True)), \
             patch.object(service, "observe_upload", AsyncMock(side_effect=["missing", "verified"])), \
             patch.object(service, "run_in_threadpool", threaded):
            result = immediate(service.upload_attachment(db, draft, identifier, owner,
                memory_upload("original.bin")))
        self.assertEqual(events, ["commit", "put", "commit"])
        self.assertTrue(result.ready_for_attach)
        self.assertEqual(result.sha256, hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(db.add.call_args.args[0].original_name, "original.bin")

    def test_draft_closed_after_reservation_is_refreshed_under_lock_and_never_puts(self):
        from app.services import challenge_attachments as service
        from fastapi import HTTPException
        owner, draft_id, identifier = uuid4(), uuid4(), uuid4()
        draft = SimpleNamespace(id=draft_id, owner_id=owner, state="open", challenge_id=17)
        db = self.db()
        calls, refreshed = [], []
        async def execute(query):
            calls.append(query)
            if len(calls) == 2:
                return SimpleNamespace(scalar_one_or_none=lambda: None)
            if len(calls) == 3:
                # 실제 ORM의 identity-map 재사용을 모사한다. refresh 옵션 없이는 old open이다.
                refreshed.append(query.get_execution_options().get("populate_existing"))
                if refreshed[-1]:
                    draft.state = "bound"
            return SimpleNamespace(scalar_one_or_none=lambda: draft)
        db.execute.side_effect = execute
        async def threaded(fn, *args):
            if fn is service.put_private_challenge_object:
                self.fail("closed draft transmitted an object PUT")
        with patch.object(service, "try_upload_lock", AsyncMock(return_value=True)), \
             patch.object(service, "observe_upload", AsyncMock(return_value="missing")) as observe, \
             patch.object(service, "run_in_threadpool", threaded), self.assertRaises(HTTPException) as caught:
            immediate(service.upload_attachment(db, draft_id, identifier, owner, memory_upload()))
        self.assertEqual(refreshed, [True])
        self.assertEqual(caught.exception.detail["code"], "attachment_draft_closed")
        self.assertEqual(db.commit.await_count, 1)
        observe.assert_not_awaited()

    def test_unknown_put_or_pending_observation_never_retries_physical_write(self):
        from fastapi import UploadFile
        from app.services import challenge_attachments as service
        for observed, put_error, expected_put in (("unknown", False, 0), ("missing", True, 1)):
            events, db = [], self.db()
            db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
            async def threaded(fn, *args):
                if fn is service.put_private_challenge_object:
                    events.append("put")
                    raise OSError("mock response lost")
            with self.subTest(observed=observed), \
                 patch.object(service, "owned_draft", AsyncMock(return_value=SimpleNamespace(state="open"))), \
                 patch.object(service, "try_upload_lock", AsyncMock(return_value=True)), \
                 patch.object(service, "observe_upload", AsyncMock(return_value=observed)), \
                 patch.object(service, "run_in_threadpool", threaded):
                result = immediate(service.upload_attachment(db, uuid4(), uuid4(), uuid4(),
                    memory_upload()))
            self.assertEqual(events.count("put"), expected_put)
            self.assertEqual((result.state, result.retry_allowed, result.ready_for_attach), ("pending", False, False))
            self.assertEqual(db.commit.await_count, 1)

    def test_reservation_commit_failure_has_zero_object_write(self):
        from fastapi import UploadFile
        from app.services import challenge_attachments as service
        db = self.db()
        db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
        db.commit.side_effect = OSError("mock commit unknown")
        from fastapi import HTTPException
        with patch.object(service, "owned_draft", AsyncMock(return_value=SimpleNamespace(state="open"))), \
             patch.object(service, "run_in_threadpool", AsyncMock()) as storage, self.assertRaises(HTTPException) as caught:
            immediate(service.upload_attachment(db, uuid4(), uuid4(), uuid4(),
                memory_upload()))
        storage.assert_not_awaited()
        self.assertEqual(caught.exception.status_code, 503)

    def test_upload_size_and_filename_rejection_precede_any_reservation(self):
        from app.services import challenge_attachments as service
        from fastapi import HTTPException
        for name, maximum, status in (("x.exe", service.MAX_FILE_SIZE, 422), ("x.bin", 2, 413)):
            db = self.db()
            with self.subTest(name=name), patch.object(service, "MAX_FILE_SIZE", maximum), self.assertRaises(HTTPException) as caught:
                immediate(service.upload_attachment(db, uuid4(), uuid4(), uuid4(), memory_upload(name)))
            self.assertEqual(caught.exception.status_code, status)
            db.execute.assert_not_awaited()
            db.commit.assert_not_awaited()

    def test_stale_legacy_only_save_conflicts_and_old_api_change_bumps_revision(self):
        from app.services import challenge_attachments as service
        from fastapi import HTTPException
        challenge = SimpleNamespace(id=17, files=["new.bin"], attachment_revision=3)
        db = self.db()
        with patch.object(service, "owned_draft", AsyncMock(return_value=SimpleNamespace(
                challenge_id=17, state="open", base_attachment_revision=2))), self.assertRaises(HTTPException) as caught:
            immediate(service.bind_attachments(db, challenge, actor_id=uuid4(),
                attachment_draft_id=uuid4(), previous_files=["old.bin"]))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(challenge.attachment_revision, 3)
        immediate(service.bind_attachments(db, challenge, actor_id=uuid4(), previous_files=["old.bin"]))
        self.assertEqual(challenge.attachment_revision, 4)
        db.commit.assert_not_awaited()

    def test_create_draft_cannot_be_reinterpreted_as_an_existing_parent_draft(self):
        from app.services import challenge_attachments as service
        from fastapi import HTTPException
        draft = SimpleNamespace(id=uuid4(), challenge_id=None, state="open", base_attachment_revision=0)
        with patch.object(service, "owned_draft", AsyncMock(return_value=draft)), self.assertRaises(HTTPException) as caught:
            immediate(service.bind_attachments(self.db(), SimpleNamespace(id=17, files=[], attachment_revision=0),
                actor_id=uuid4(), attachment_ids=[], attachment_draft_id=draft.id, previous_files=[]))
        self.assertEqual(caught.exception.detail["code"], "attachment_draft_target_conflict")

    def test_foreign_parent_reference_rejected_before_any_link_change(self):
        from app.services import challenge_attachments as service
        from fastapi import HTTPException
        row = self.asset(bound_challenge_id=18, challenge_id=18)
        db = self.db()
        db.execute.side_effect = [rows_result([]), rows_result([row])]
        with self.assertRaises(HTTPException) as caught:
            immediate(service.bind_attachments(db, SimpleNamespace(id=17, files=[], attachment_revision=0),
                actor_id=uuid4(), attachment_ids=[row.id], previous_files=[]))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual((row.challenge_id, row.bound_challenge_id), (18, 18))
        db.commit.assert_not_awaited()

    def test_same_parent_export_refs_can_reorder_without_staged_owner_authority(self):
        from app.services import challenge_attachments as service
        rows = [self.asset(challenge_id=17, bound_challenge_id=17, position=i) for i in range(2)]
        for row in rows:
            row.object_path = service.private_path(row.owner_id, row.draft_id, row.id)
        db = self.db()
        db.execute.side_effect = [rows_result(rows), rows_result(rows)]
        parent = SimpleNamespace(id=17, files=["kept.bin"], attachment_revision=3)
        with patch.object(service, "observe_upload", AsyncMock()) as storage:
            immediate(service.bind_attachments(db, parent, actor_id=uuid4(),
                attachment_ids=[row.id for row in reversed(rows)], previous_files=["kept.bin"]))
        self.assertEqual(parent.attachment_revision, 4)
        self.assertEqual([row.position for row in rows], [1, 0])
        self.assertEqual(parent.files, ["kept.bin"])
        storage.assert_not_awaited()
        db.commit.assert_not_awaited()

    def test_second_object_failure_cannot_partially_detach_or_attach_parent(self):
        from app.services import challenge_attachments as service
        from fastapi import HTTPException
        owner, draft_id = uuid4(), uuid4()
        current = self.asset(challenge_id=17, bound_challenge_id=17)
        staged = [self.asset(owner_id=owner, draft_id=draft_id) for _ in range(2)]
        for row in [current, *staged]:
            row.object_path = service.private_path(row.owner_id, row.draft_id, row.id)
        db = self.db()
        db.execute.side_effect = [rows_result([current]), rows_result([current, *staged])]
        draft = SimpleNamespace(id=draft_id, challenge_id=17, state="open", base_attachment_revision=3)
        parent = SimpleNamespace(id=17, files=[], attachment_revision=3)
        with patch.object(service, "owned_draft", AsyncMock(return_value=draft)), \
             patch.object(service, "observe_upload", AsyncMock(side_effect=["verified", "unknown"])), \
             self.assertRaises(HTTPException) as caught:
            immediate(service.bind_attachments(db, parent, actor_id=owner, attachment_draft_id=draft_id,
                attachment_ids=[row.id for row in staged], previous_files=[]))
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(current.challenge_id, 17)
        self.assertTrue(all(row.challenge_id is None and row.bound_challenge_id is None for row in staged))
        self.assertEqual((parent.attachment_revision, draft.state), (3, "open"))
        db.commit.assert_not_awaited()

    def test_pending_copy_failure_keeps_original_path_and_parent_unbound(self):
        from app.services import challenge_attachments as service
        from fastapi import HTTPException
        submission_id, owner = uuid4(), uuid4()
        source = f"challenge-pending/misc/easy/{submission_id}/owned.bin"
        asset = self.asset(owner_id=owner, draft_id=submission_id, object_path=source, state="pending")
        db = self.db([asset])
        sub = SimpleNamespace(id=submission_id, submitter_id=owner, file_paths=[source])
        challenge = SimpleNamespace(id=17, files=None, attachment_revision=0)
        async def failed_copy(fn, *args):
            self.assertIs(fn, service.copy_pending_challenge_object)
            raise OSError("mock copy denied")
        with patch.object(service, "run_in_threadpool", failed_copy), self.assertRaises(HTTPException) as caught:
            immediate(service.prepare_submission_attachments(db, sub, challenge))
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual((asset.object_path, asset.state, asset.challenge_id), (source, "pending", None))
        db.commit.assert_not_awaited()

    def test_approval_copy_success_binds_same_transaction_and_retains_pending_source(self):
        from app.services import challenge_attachments as service
        submission_id, owner = uuid4(), uuid4()
        source = f"challenge-pending/misc/easy/{submission_id}/owned.bin"
        asset = self.asset(owner_id=owner, draft_id=submission_id, object_path=source, state="pending")
        draft = SimpleNamespace(challenge_id=None, state="open")
        db = self.db([asset])
        db.get = AsyncMock(return_value=draft)
        sub = SimpleNamespace(id=submission_id, submitter_id=owner, file_paths=[source])
        parent = SimpleNamespace(id=17, files=None, attachment_revision=0)
        calls = []
        async def storage(fn, *args):
            calls.append((fn, args))
            return "verified" if fn is service.verify_challenge_object else None
        with patch.object(service, "run_in_threadpool", storage):
            immediate(service.prepare_submission_attachments(db, sub, parent))
        self.assertEqual([call[0] for call in calls], [service.copy_pending_challenge_object, service.verify_challenge_object])
        self.assertEqual(calls[0][1][0], source)
        self.assertEqual(sub.file_paths, [source])
        self.assertEqual((asset.challenge_id, asset.bound_challenge_id, asset.original_name), (17, 17, "sample.bin"))
        self.assertEqual((draft.challenge_id, draft.state, parent.attachment_revision), (17, "bound", 1))
        db.commit.assert_not_awaited()

    def test_approval_copy_failure_rolls_back_without_approving_or_commit(self):
        from app.api import submissions
        from fastapi import HTTPException
        sub = SimpleNamespace(id=uuid4(), status="pending", flag_type="static", description="", hints=[],
            flag_storage_version="legacy-raw", flag_ciphertext=None, flag_value="mock",
            name="AP1", category="misc", difficulty="easy", value=100, tags=[],
            submitter_id=uuid4(), submitter_name="AP1", file_paths=["owned"])
        db = self.db()
        db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: sub)
        with patch.object(submissions, "prepare_submission_attachments", AsyncMock(side_effect=OSError("mock copy failure"))), \
             self.assertRaises(HTTPException) as caught:
            immediate(submissions.approve_submission(str(sub.id), db=db, admin_id=uuid4(), _role="admin"))
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(sub.status, "pending")
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()

    def test_private_bucket_policy_and_exact_missing_classification_preserve_existing_policy(self):
        from app.utils import minio_client as storage
        from minio.error import S3Error
        client = Mock()
        client.bucket_exists.return_value = True
        client.get_bucket_policy.return_value = '{"Statement":[{"Effect":"Allow","Principal":"*"}]}'
        with patch.object(storage, "get_minio_client", return_value=client), self.assertRaises(RuntimeError):
            storage.ensure_challenge_private_bucket()
        client.set_bucket_policy.assert_not_called()
        for code, missing in (("NoSuchKey", True), ("NoSuchBucket", False), ("AccessDenied", False)):
            client.stat_object.side_effect = S3Error(code, "mock", "/", "request", "host", None)
            with self.subTest(code=code), patch.object(storage, "get_minio_client", return_value=client):
                if missing:
                    self.assertIsNone(storage.stat_challenge_object("challenges/a"))
                else:
                    with self.assertRaises(S3Error):
                        storage.stat_challenge_object("challenges/a")

    def test_storage_hash_mismatch_is_not_missing_and_stream_always_closes(self):
        from app.utils import minio_client as storage
        client, response = Mock(), Mock()
        client.stat_object.return_value = SimpleNamespace(size=3)
        client.get_object.return_value = response
        response.stream.return_value = [b"bad"]
        with patch.object(storage, "get_minio_client", return_value=client), self.assertRaises(ValueError):
            storage.verify_challenge_object("challenge-private-assets/attachments/owned", 3, "a" * 64)
        response.close.assert_called_once()
        response.release_conn.assert_called_once()

    def test_pending_resolver_rejects_foreign_submission_or_bucket_before_storage(self):
        from app.services.challenge_attachments import pending_object_path
        identifier = uuid4()
        self.assertIsNotNone(pending_object_path(f"challenge-pending/misc/easy/{identifier}/x.bin", identifier))
        for path in (f"education-private-assets/misc/easy/{identifier}/x.bin",
                     f"challenge-pending/misc/easy/{uuid4()}/x.bin",
                     f"challenge-pending/misc/easy/{identifier}/../x.bin"):
            self.assertIsNone(pending_object_path(path, identifier))


if __name__ == "__main__":
    unittest.main()
