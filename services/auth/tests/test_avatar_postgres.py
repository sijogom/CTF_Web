"""실제 PG16의 소유 UUID schema만 사용한다. MinIO는 fake이며 HTTP/SMTP는 없다."""

import asyncio
import os
import re
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import me
from app.models.user import Base, User
from app.utils import minio_client as storage
from test_avatar_safety import File, Store
from test_competition_team_postgres import validate_work_database_url


TEST_URL = os.environ.get("AVATAR_TEST_DATABASE_URL", "")


class AvatarDatabaseGuardTests(unittest.TestCase):
    def test_only_loopback_owned_database_and_identical_role_are_accepted(self):
        identity = "ctfi_a_" + uuid4().hex
        value = f"postgresql+asyncpg://{identity}:dummy@127.0.0.1:15433/{identity}"
        self.assertEqual(validate_work_database_url(value).database, identity)
        for invalid in (value.replace("127.0.0.1", "localhost"), value.replace(":15433", ":5432"),
                        value + "?sslmode=disable", value.replace(identity, "platform_db"),
                        value.rsplit("/", 1)[0] + "/ctfi_a_" + uuid4().hex):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                validate_work_database_url(invalid)


@unittest.skipUnless(TEST_URL, "새 소유 ctfi_a UUID PG16 URL인 AVATAR_TEST_DATABASE_URL이 필요합니다.")
class PostgreSQLAvatarTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = validate_work_database_url(TEST_URL)
        self.schema = "avatar_test_" + uuid4().hex
        self.schema_oid, self.engine = None, None
        self.admin = create_async_engine(TEST_URL, echo=False, hide_parameters=True, connect_args={"server_settings": {
            "search_path": "pg_catalog", "application_name": self.schema + "_guard",
            "statement_timeout": "15000", "lock_timeout": "4000",
        }})
        self.addAsyncCleanup(self._cleanup)
        async with self.admin.begin() as connection:
            identity = (await connection.execute(text(
                "SELECT current_database(),current_user,current_setting('server_version_num')::int,"
                "host(inet_server_addr()),inet_server_port(),"
                "(SELECT datdba=(SELECT oid FROM pg_roles WHERE rolname=current_user) FROM pg_database WHERE datname=current_database()),"
                "(SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user)"
            ))).one()
            self.assertEqual(tuple(identity[:2]), (url.database, url.username))
            self.assertTrue(160000 <= identity[2] < 170000)
            self.assertEqual(tuple(identity[3:5]), ("127.0.0.1", 15433))
            self.assertTrue(identity[5])
            self.assertFalse(identity[6])
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
            oid = (await connection.execute(text(
                "SELECT oid FROM pg_namespace WHERE nspname=:schema AND nspowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)"
            ), {"schema": self.schema})).scalar_one()
        self.schema_oid = oid
        self.engine = create_async_engine(TEST_URL, echo=False, hide_parameters=True, connect_args={"server_settings": {
            "search_path": self.schema, "application_name": self.schema,
            "statement_timeout": "15000", "lock_timeout": "4000",
        }})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.owner = uuid4()
        self.old_name = f"{self.owner}.png"
        self.old_url = f"/ctf/storage/avatars/{self.old_name}"
        async with self.engine.begin() as connection:
            self.assertEqual((await connection.execute(text("SELECT current_schema()"))).scalar_one(), self.schema)
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[User.__table__]))
        async with self.sessions() as db:
            db.add(User(id=self.owner, username="owned_" + uuid4().hex[:16], email=f"{uuid4().hex}@cju.ac.kr",
                        email_verified=True, avatar_url=self.old_url))
            await db.commit()
        self.store = Store([])
        self.store.objects["avatars", self.old_name] = b"old PG fixture bytes"
        self.client_patch = patch.object(storage, "get_minio_client", return_value=self.store)
        self.client_patch.start()
        self.env_patch = patch.dict(os.environ, {"COOKIE_BASE_PATH": "/ctf"})
        self.env_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.addCleanup(self.env_patch.stop)

    async def _cleanup(self):
        if self.engine is not None:
            await self.engine.dispose()
        try:
            if self.schema_oid is not None:
                self.assertRegex(self.schema, r"^avatar_test_[0-9a-f]{32}$")
                async with self.admin.begin() as connection:
                    row = (await connection.execute(text(
                        "SELECT oid,nspowner=(SELECT oid FROM pg_roles WHERE rolname=current_user) AS owned "
                        "FROM pg_namespace WHERE nspname=:schema"
                    ), {"schema": self.schema})).one_or_none()
                    self.assertIsNotNone(row)
                    self.assertEqual(row.oid, self.schema_oid)
                    self.assertTrue(row.owned)
                    await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        finally:
            await self.admin.dispose()

    async def pointer(self):
        async with self.sessions() as db:
            return (await db.execute(select(User.avatar_url).where(User.id == self.owner))).scalar_one()

    async def test_cached_dependency_is_refreshed_after_another_session_commits(self):
        newer_name = f"{self.owner}/{uuid4().hex}.webp"
        newer_url = f"/ctf/storage/avatars/{newer_name}"
        self.store.objects["avatars", newer_name] = b"other completed request"
        async with self.sessions() as stale, self.sessions() as other:
            dependency = await stale.get(User, self.owner)
            current = await other.get(User, self.owner)
            current.avatar_url = newer_url
            await other.commit()
            self.assertEqual(dependency.avatar_url, self.old_url)
            result = await asyncio.wait_for(me.update_avatar(file=File(), user=dependency, db=stale), 8)
        self.assertEqual(await self.pointer(), result["avatar_url"])
        self.assertEqual(self.store.deletes, [("avatars", newer_name)])
        self.assertIn(("avatars", self.old_name), self.store.objects)
        self.assertIn(("avatars", self.store.puts[0][1]), self.store.objects)

    async def test_remove_and_pending_upload_serialize_on_the_actual_user_row(self):
        pending = None
        async with self.sessions() as holder, self.sessions() as waiter:
            owner = (await holder.execute(select(User).where(User.id == self.owner).with_for_update())).scalar_one()
            holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            dependency = await waiter.get(User, self.owner)
            waiter_pid = (await waiter.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            try:
                pending = asyncio.create_task(me.update_avatar(file=File(), user=dependency, db=waiter))
                async def blocked():
                    async with self.admin.connect() as connection:
                        while not (await connection.execute(text("SELECT :holder=ANY(pg_blocking_pids(:waiter))"),
                                                            {"holder": holder_pid, "waiter": waiter_pid})).scalar_one():
                            await asyncio.sleep(0.01)
                await asyncio.wait_for(blocked(), 2)
                self.assertFalse(pending.done())
                await me.remove_avatar(user=owner, db=holder)
                result = await asyncio.wait_for(pending, 6)
            finally:
                if pending is not None and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
        self.assertEqual(await self.pointer(), result["avatar_url"])
        self.assertEqual(self.store.deletes, [("avatars", self.old_name)])
        self.assertIn(("avatars", self.store.puts[0][1]), self.store.objects)

    async def test_real_update_failure_rolls_back_pointer_and_preserves_both_objects(self):
        self.assertIsNotNone(re.fullmatch(r"avatar_test_[0-9a-f]{32}", self.schema))
        async with self.engine.begin() as connection:
            await connection.execute(text(f'CREATE FUNCTION "{self.schema}".owned_avatar_reject() RETURNS trigger LANGUAGE plpgsql '
                                          "AS $$ BEGIN RAISE EXCEPTION 'owned avatar update rejection' USING ERRCODE='23514'; END; $$"))
            await connection.execute(text(f'CREATE TRIGGER owned_avatar_reject BEFORE UPDATE OF avatar_url ON "{self.schema}".users '
                                          f'FOR EACH ROW EXECUTE FUNCTION "{self.schema}".owned_avatar_reject()'))
        for remove in (False, True):
            async with self.sessions() as db:
                user = await db.get(User, self.owner)
                with self.assertRaises(HTTPException) as error:
                    if remove:
                        await me.remove_avatar(user=user, db=db)
                    else:
                        await me.update_avatar(file=File(), user=user, db=db)
                self.assertEqual(error.exception.status_code, 503)
                self.assertNotIn("owned avatar update rejection", error.exception.detail)
            self.assertEqual(await self.pointer(), self.old_url)
        self.assertEqual(self.store.deletes, [])
        self.assertIn(("avatars", self.old_name), self.store.objects)
        self.assertIn(("avatars", self.store.puts[0][1]), self.store.objects)


if __name__ == "__main__":
    unittest.main()
