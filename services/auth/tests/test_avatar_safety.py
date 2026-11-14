"""아바타 경계의 순수 회귀. DB/SDK는 명시 fake이며 외부 await를 실행하지 않는다."""

import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.api import me
from app.utils import minio_client as storage


def immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError("순수 검사에서 외부 대기점이 발생했습니다.")


class File:
    def __init__(self, data=b"owned image bytes", filename="avatar.png"):
        self.data, self.filename = data, filename
        self.read_sizes = []

    async def read(self, size):
        self.read_sizes.append(size)
        return self.data[:size]


class Store:
    def __init__(self, events):
        self.events, self.objects, self.puts, self.deletes = events, {}, [], []
        self.put_failure = None
        self.delete_failure = False

    def bucket_exists(self, bucket):
        return True

    def put_object(self, bucket, name, stream, *, length, content_type):
        self.events.append("put")
        if self.put_failure == "before":
            raise OSError("private SDK detail")
        data = stream.read()
        if len(data) != length:
            raise AssertionError("SDK bytes 경계 불일치")
        self.objects[bucket, name] = data
        self.puts.append((bucket, name, content_type))
        if self.put_failure == "after":
            raise OSError("private SDK detail")
        if self.put_failure == "after_value":
            raise ValueError("private SDK detail")
        if self.put_failure == "cancel":
            raise asyncio.CancelledError()

    def remove_object(self, bucket, name):
        self.events.append("delete")
        self.deletes.append((bucket, name))
        if self.delete_failure:
            raise OSError("private cleanup detail")
        self.objects.pop((bucket, name), None)


class Database:
    def __init__(self, owner, avatar_url, events):
        self.id, self.avatar_url, self.events = owner, avatar_url, events
        self.is_active, self.missing = True, False
        self.statements, self.rollbacks = [], 0
        self.failure, self.rollback_failure = None, False

    async def execute(self, statement):
        self.events.append("lock")
        self.statements.append(statement)
        if self.failure == "lock_cancel":
            raise asyncio.CancelledError()
        self.row = SimpleNamespace(id=self.id, avatar_url=self.avatar_url, is_active=self.is_active)
        return SimpleNamespace(scalar_one_or_none=lambda: None if self.missing else self.row)

    async def commit(self):
        self.events.append("commit")
        if self.failure == "before":
            raise OSError("private DB detail")
        self.avatar_url = self.row.avatar_url
        if self.failure == "after":
            raise OSError("private DB detail")
        if self.failure == "cancel":
            raise asyncio.CancelledError()

    async def rollback(self):
        self.events.append("rollback")
        self.rollbacks += 1
        if self.rollback_failure:
            raise OSError("private rollback detail")


class AvatarSafetyTests(unittest.TestCase):
    def setUp(self):
        self.owner = uuid4()
        self.events = []
        self.old_name = f"{self.owner}.png"
        self.old_url = f"/ctf/storage/avatars/{self.old_name}"
        self.user = SimpleNamespace(id=self.owner, avatar_url=self.old_url)
        self.db = Database(self.owner, self.old_url, self.events)
        self.store = Store(self.events)
        self.store.objects["avatars", self.old_name] = b"old original bytes"
        client_patch = patch.object(storage, "get_minio_client", return_value=self.store)
        self.client = client_patch.start()
        self.addCleanup(client_patch.stop)
        env_patch = patch.dict(os.environ, {"COOKIE_BASE_PATH": "/ctf"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def upload(self, file=None):
        return immediate(me.update_avatar(file=file or File(), user=self.user, db=self.db))

    def test_cleanup_preserves_patches_owned_by_the_caller(self):
        target = SimpleNamespace(guard="original")
        external = patch.object(target, "guard", "external")
        external.start()
        nested = AvatarSafetyTests("test_read_cancellation_happens_before_any_storage_or_database_work")
        try:
            nested.setUp()
            nested.doCleanups()
            self.assertEqual(target.guard, "external")
            self.assertIs(storage.get_minio_client, self.client)
        finally:
            nested.doCleanups()
            external.stop()

    def assert_failure_preserved(self, status=None):
        if status is not None:
            self.assertEqual(status, 503)
        self.assertEqual(self.store.objects["avatars", self.old_name], b"old original bytes")
        self.assertEqual(self.store.deletes, [])

    def test_same_extension_always_uses_distinct_owned_uuid_objects_and_original_bytes(self):
        first = storage.upload_avatar(b"first", "one.PNG", str(self.owner))
        second = storage.upload_avatar(b"second", "two.png", str(self.owner))
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.objects["avatars", first.split("/", 1)[1]], b"first")
        self.assertEqual(self.store.objects["avatars", self.old_name], b"old original bytes")
        for value in (first, second):
            self.assertRegex(value, rf"^avatars/{self.owner}/[0-9a-f]{{32}}\.png$")

    def test_five_extensions_and_mime_mapping_are_unchanged(self):
        expected = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
        self.assertEqual(storage.AVATAR_MAX_SIZE, 2 * 1024 * 1024)
        for extension, mime in expected.items():
            storage.upload_avatar(b"original", "owned." + extension, str(self.owner))
            self.assertEqual(self.store.puts[-1][2], mime)

    def test_invalid_input_is_rejected_before_sdk_and_database(self):
        for file in (File(filename="bad.txt"), File(filename=""), File(b"x" * (storage.AVATAR_MAX_SIZE + 1))):
            with self.subTest(filename=file.filename), self.assertRaises(HTTPException) as error:
                self.upload(file)
            self.assertEqual(error.exception.status_code, 400)
        self.client.assert_not_called()
        self.assertEqual(self.events, [])
        self.assertEqual(self.db.avatar_url, self.old_url)

    def test_exact_size_limit_and_bounded_upload_read(self):
        file = File(b"x" * storage.AVATAR_MAX_SIZE)
        self.upload(file)
        self.assertEqual(file.read_sizes, [storage.AVATAR_MAX_SIZE + 1])
        self.assertEqual(len(self.store.objects["avatars", self.store.puts[0][1]]), storage.AVATAR_MAX_SIZE)

    def test_lock_refresh_commit_precedes_precise_old_cleanup(self):
        result = self.upload()
        self.assertEqual(self.events, ["put", "lock", "commit", "delete"])
        self.assertEqual(result, {"avatar_url": self.db.avatar_url})
        self.assertNotIn(("avatars", self.old_name), self.store.objects)
        statement = self.db.statements[0]
        self.assertTrue(statement.get_execution_options()["populate_existing"])
        compiled = statement.compile(dialect=postgresql.dialect())
        self.assertIn("FOR UPDATE", str(compiled))
        self.assertEqual(compiled.params["id_1"], self.owner)

    def test_stale_dependency_cannot_select_the_cleanup_pointer(self):
        fresh_name = f"{self.owner}/{uuid4().hex}.webp"
        self.db.avatar_url = f"/ctf/storage/avatars/{fresh_name}"
        self.store.objects["avatars", fresh_name] = b"other completed request"
        self.upload()
        self.assertEqual(self.store.deletes, [("avatars", fresh_name)])
        self.assertIn(("avatars", self.old_name), self.store.objects)
        self.assertIn(("avatars", self.store.puts[0][1]), self.store.objects)

    def test_sdk_failure_before_write_preserves_old_pointer_and_bytes(self):
        self.store.put_failure = "before"
        with self.assertRaises(HTTPException) as error:
            self.upload()
        self.assert_failure_preserved(error.exception.status_code)
        self.assertEqual(self.db.avatar_url, self.old_url)
        self.assertEqual(self.db.statements, [])
        self.assertNotIn("private", error.exception.detail)

    def test_sdk_failure_after_write_preserves_both_objects_and_is_not_validation_400(self):
        for failure in ("after", "after_value"):
            with self.subTest(failure=failure):
                self.store.put_failure = failure
                with self.assertRaises(HTTPException) as error:
                    self.upload()
                self.assert_failure_preserved(error.exception.status_code)
                self.assertIn(("avatars", self.store.puts[-1][1]), self.store.objects)
                self.assertNotIn("private", error.exception.detail)
        self.assertEqual(self.db.statements, [])

    def test_commit_failure_before_application_retains_old_and_staged_bytes(self):
        self.db.failure = "before"
        with self.assertRaises(HTTPException) as error:
            self.upload()
        self.assert_failure_preserved(error.exception.status_code)
        self.assertEqual(self.db.avatar_url, self.old_url)
        self.assertEqual(self.db.rollbacks, 1)
        self.assertIn(("avatars", self.store.puts[0][1]), self.store.objects)
        self.assertNotIn("private", error.exception.detail)

    def test_commit_applied_then_response_failure_does_not_delete_either_object(self):
        self.db.failure = "after"
        with self.assertRaises(HTTPException) as error:
            self.upload()
        self.assert_failure_preserved(error.exception.status_code)
        self.assertNotEqual(self.db.avatar_url, self.old_url)
        self.assertIn(("avatars", self.store.puts[0][1]), self.store.objects)

    def test_rollback_failure_does_not_replace_safe_original_error(self):
        self.db.failure, self.db.rollback_failure = "before", True
        with self.assertRaises(HTTPException) as error:
            self.upload()
        self.assert_failure_preserved(error.exception.status_code)
        self.assertNotIn("private", error.exception.detail)

    def test_cancellation_at_sdk_lock_and_commit_never_cleans_objects(self):
        for failure in ("sdk", "lock_cancel", "cancel"):
            with self.subTest(failure=failure):
                self.store.put_failure = "cancel" if failure == "sdk" else None
                self.db.failure = None if failure == "sdk" else failure
                with self.assertRaises(asyncio.CancelledError):
                    self.upload()
                self.assert_failure_preserved()
        self.assertGreaterEqual(len(self.store.puts), 3)

    def test_fresh_missing_or_inactive_account_rejects_without_cleanup(self):
        for missing in (False, True):
            self.db.missing, self.db.is_active = missing, False
            with self.subTest(missing=missing), self.assertRaises(HTTPException) as error:
                self.upload()
            self.assertEqual(error.exception.status_code, 401)
            self.assertEqual(self.store.deletes, [])

    def test_cleanup_failure_cannot_reverse_confirmed_upload(self):
        self.store.delete_failure = True
        result = self.upload()
        self.assertEqual(result["avatar_url"], self.db.avatar_url)
        self.assertEqual(self.db.rollbacks, 0)
        self.assertIn(("avatars", self.old_name), self.store.objects)

    def test_read_cancellation_happens_before_any_storage_or_database_work(self):
        class CancelledFile(File):
            async def read(self, size):
                raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            self.upload(CancelledFile())
        self.assertEqual(self.events, [])
        self.client.assert_not_called()

    def test_remove_commits_null_before_cleanup_and_repeats_without_sdk_delete(self):
        self.assertEqual(immediate(me.remove_avatar(user=self.user, db=self.db)), {"avatar_url": None})
        self.assertEqual(self.events, ["lock", "commit", "delete"])
        self.assertIsNone(self.db.avatar_url)
        immediate(me.remove_avatar(user=self.user, db=self.db))
        self.assertEqual(self.store.deletes, [("avatars", self.old_name)])

    def test_remove_commit_failure_or_cancellation_preserves_old_bytes(self):
        for failure, expected in (("before", HTTPException), ("after", HTTPException), ("cancel", asyncio.CancelledError)):
            self.db.avatar_url, self.db.failure = self.old_url, failure
            with self.subTest(failure=failure), self.assertRaises(expected) as error:
                immediate(me.remove_avatar(user=self.user, db=self.db))
            if isinstance(error.exception, HTTPException):
                self.assertEqual(error.exception.status_code, 503)
            self.assert_failure_preserved()

    def test_remove_cleanup_failure_keeps_confirmed_null_pointer(self):
        self.store.delete_failure = True
        self.assertEqual(immediate(me.remove_avatar(user=self.user, db=self.db)), {"avatar_url": None})
        self.assertIsNone(self.db.avatar_url)
        self.assertEqual(self.db.rollbacks, 0)
        self.assertIn(("avatars", self.old_name), self.store.objects)

    def test_cleanup_accepts_only_exact_current_owner_legacy_or_uuid_paths(self):
        new_name = f"{self.owner}/{uuid4().hex}.jpeg"
        for prefix in ("/storage/avatars/", "/ctf/storage/avatars/"):
            for name in (self.old_name, new_name):
                self.assertEqual(storage.owned_avatar_object_name(prefix + name, str(self.owner)), name)

    def test_foreign_external_encoded_malformed_paths_never_reach_sdk(self):
        other = uuid4()
        bad = [None, "", "//[", "http://[", "https://foreign.test" + self.old_url,
               f"/ctf/storage/avatars/{other}.png", f"/storage/banners/{self.old_name}",
               "avatars/" + self.old_name, self.old_url + "?x=1", self.old_url + "#x",
               self.old_url.replace("avatars/", "avatars/../"), self.old_url.replace("/avatars/", "/avatars%2f/"),
               self.old_url.replace("/avatars/", "/avatars\\"), "/other" + self.old_url,
               self.old_url + "\n", f"/ctf/storage/avatars/{self.owner}/{'0' * 32}.png"]
        for value in bad:
            with self.subTest(value=value):
                self.assertFalse(storage.delete_avatar(value, str(self.owner)))
        self.client.assert_not_called()

    def test_unknown_legacy_pointer_does_not_prevent_new_pointer_or_remove(self):
        self.db.avatar_url = "https://foreign.test/custom.png"
        self.upload()
        self.assertEqual(self.store.deletes, [])
        self.db.avatar_url = "https://foreign.test/custom.png"
        self.assertEqual(immediate(me.remove_avatar(user=self.user, db=self.db)), {"avatar_url": None})
        self.assertEqual(self.store.deletes, [])


if __name__ == "__main__":
    unittest.main()
