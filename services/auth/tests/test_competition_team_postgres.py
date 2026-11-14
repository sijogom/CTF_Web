"""실제 PG16 전용: 새 UUID schema의 합성 행만 쓰며 HTTP/SMTP/운영 자료는 사용하지 않는다."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.competition import (
    Competition, CompetitionAssignment, CompetitionChallenge, CompetitionParticipant,
    CompetitionSolve, CompetitionTeamAssignment, CompetitionTeamMember,
    CompetitionTeamRegistration, CompetitionTeamSolve,
)
from app.models.team import Team, TeamMember
from app.models.user import Base, User
from app.services import competition_team as service


TEST_URL = os.environ.get("COMPETITION_TEAM_TEST_DATABASE_URL", "")
TEAM_TABLES = (CompetitionTeamRegistration, CompetitionTeamMember, CompetitionTeamAssignment, CompetitionTeamSolve)


def validate_work_database_url(value):
    message = "새 작업 소유 ctfi_a UUID DB와 같은 이름의 전용 role만 허용합니다."
    if not isinstance(value, str) or not re.fullmatch(
        r"postgresql\+asyncpg://(?P<identity>ctfi_a_[0-9a-f]{32}):[^/?#@]+"
        r"@127\.0\.0\.1:15433/(?P=identity)", value
    ):
        raise ValueError(message)
    url = make_url(value)
    if (url.query or url.drivername != "postgresql+asyncpg"
            or url.host != "127.0.0.1" or url.port != 15433
            or not re.fullmatch(r"ctfi_a_[0-9a-f]{32}", url.database or "")
            or url.username != url.database):
        raise ValueError(message)
    return url


@unittest.skipUnless(TEST_URL, "작업 전용 PG16 URL인 COMPETITION_TEAM_TEST_DATABASE_URL이 필요합니다.")
class PostgreSQLCompetitionTeamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = validate_work_database_url(TEST_URL)
        self.schema = "competition_team_test_" + uuid4().hex
        self.assertRegex(self.schema, r"^competition_team_test_[0-9a-f]{32}$")
        self.created_schema = False
        self.schema_oid = None
        self.engine = None
        self.admin = create_async_engine(TEST_URL, echo=False, connect_args={"server_settings": {
            "search_path": "pg_catalog", "application_name": self.schema + "_admin",
            "statement_timeout": "15000", "lock_timeout": "5000",
        }})
        self.addAsyncCleanup(self._cleanup_schema)
        async with self.admin.begin() as connection:
            identity = (await connection.execute(text(
                "SELECT current_database(),current_user,current_setting('server_version_num')::int"
            ))).one()
            self.assertEqual(tuple(identity[:2]), (url.database, url.username))
            version = int(identity[2])
            self.assertGreaterEqual(version, 160000)
            self.assertLess(version, 170000)
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
            self.schema_oid = (await connection.execute(text(
                "SELECT oid FROM pg_namespace WHERE nspname = :schema AND nspowner = (SELECT usesysid FROM pg_user WHERE usename = current_user)"
            ), {"schema": self.schema})).scalar_one()
        self.created_schema = True
        self.engine = create_async_engine(TEST_URL, echo=False, connect_args={"server_settings": {
            "search_path": self.schema, "application_name": self.schema,
            "statement_timeout": "15000", "lock_timeout": "5000",
        }})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.users = [uuid4() for _ in range(4)]
        self.names = {user_id: f"owned_{index}_{uuid4().hex[:8]}" for index, user_id in enumerate(self.users)}
        self.team_a, self.team_b, self.legacy, self.competition = [uuid4() for _ in range(4)]
        self.team_name = "owned-team-" + uuid4().hex[:8]
        self.now = datetime.now(timezone.utc)
        async with self.engine.begin() as connection:
            self.assertEqual((await connection.execute(text("SELECT current_schema()"))).scalar_one(), self.schema)
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
                User.__table__, Team.__table__, TeamMember.__table__, Competition.__table__,
                CompetitionChallenge.__table__, CompetitionParticipant.__table__,
                CompetitionAssignment.__table__, CompetitionSolve.__table__,
            ]))
        async with self.sessions() as db:
            db.add_all([User(id=user_id, email=f"{uuid4().hex}@example.invalid", username=self.names[user_id],
                             email_verified=True, xp=17) for user_id in self.users])
            await db.flush()
            db.add_all([Team(id=self.team_a, name=self.team_name, captain_id=self.users[0], total_score=23),
                        Team(id=self.team_b, name="other-" + uuid4().hex[:8], captain_id=self.users[2], total_score=29),
                        self._competition(self.legacy, marker=None)])
            await db.flush()
            db.add_all([TeamMember(team_id=self.team_a, user_id=self.users[0]),
                        TeamMember(team_id=self.team_a, user_id=self.users[1]),
                        TeamMember(team_id=self.team_b, user_id=self.users[2]),
                        CompetitionParticipant(competition_id=self.legacy, user_id=self.users[0], score=31),
                        CompetitionAssignment(competition_id=self.legacy, user_id=self.users[0], category="Web", challenge_ids=[21]),
                        CompetitionSolve(competition_id=self.legacy, user_id=self.users[0], challenge_id=21, solved_at=self.now)])
            await db.commit()
        self.legacy_before = await self._legacy_snapshot()
        # 005 구조에서 실제 006의 ADD COLUMN/새 원장/트리거 DDL을 실행한다.
        async with self.engine.begin() as connection:
            await connection.execute(text('ALTER TABLE competitions DROP CONSTRAINT ck_competitions_team_contract'))
            await connection.execute(text('ALTER TABLE competitions DROP COLUMN team_contract'))
        migration_path = Path(__file__).parents[1] / "migrations/versions/20261018_006_competition_teams.py"
        spec = importlib.util.spec_from_file_location("owned_competition_team_migration_" + uuid4().hex, migration_path)
        self.migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.migration)
        await self._migrate("upgrade")
        self.assertEqual(await self._legacy_snapshot(), self.legacy_before)
        async with self.sessions() as db:
            db.add(self._competition(self.competition, marker=service.FIXED_ROSTER_CONTRACT))
            await db.flush()
            db.add_all([CompetitionChallenge(competition_id=self.competition, challenge_id=challenge_id,
                                            category="Web", difficulty="easy") for challenge_id in range(21, 28)])
            await db.commit()

    def _competition(self, competition_id, *, marker):
        return Competition(id=competition_id, title="owned integration", mode="team", team_contract=marker,
                           start_at=self.now - timedelta(hours=1), end_at=self.now + timedelta(hours=1),
                           created_by=self.users[0], is_published=True)

    async def _cleanup_schema(self):
        if self.engine is not None:
            await self.engine.dispose()
        try:
            if self.created_schema:
                self.assertIsNotNone(re.fullmatch(r"competition_team_test_[0-9a-f]{32}", self.schema))
                async with self.admin.begin() as connection:
                    row = (await connection.execute(text(
                        "SELECT oid, nspowner = (SELECT usesysid FROM pg_user WHERE usename = current_user) AS owned FROM pg_namespace WHERE nspname = :schema"
                    ), {"schema": self.schema})).one_or_none()
                    if row is not None:
                        self.assertEqual(row.oid, self.schema_oid)
                        self.assertTrue(row.owned)
                        await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        finally:
            await self.admin.dispose()

    async def _migrate(self, direction):
        async with self.engine.begin() as connection:
            def apply(sync):
                with patch.object(self.migration, "op", Operations(MigrationContext.configure(sync))):
                    getattr(self.migration, direction)()
            await connection.run_sync(apply)

    async def _legacy_snapshot(self):
        result = {}
        async with self.engine.connect() as connection:
            for table in ("competitions", "competition_participants", "competition_assignments", "competition_solves"):
                key = "id" if table == "competitions" else "competition_id"
                projection = "to_jsonb(t) - 'team_contract'" if table == "competitions" else "to_jsonb(t)"
                rows = (await connection.execute(text(f"SELECT ({projection})::text FROM {table} t WHERE {key} = :competition ORDER BY 1"),
                                                 {"competition": self.legacy})).scalars().all()
                result[table] = [json.loads(row) for row in rows]
        return result

    async def _counts(self):
        async with self.sessions() as db:
            return {model.__tablename__: (await db.execute(select(func.count()).select_from(model).where(
                model.competition_id == self.competition))).scalar_one() for model in (*TEAM_TABLES, CompetitionSolve)}

    async def _register(self):
        async with self.sessions() as db:
            registration = await service.register_team(db, self.competition, self.team_a, self.users[0])
            await db.commit()
            return registration

    async def _assign_one(self):
        await self._register()
        async with self.sessions() as db:
            assignment = await service.get_or_create_team_assignment(db, self.competition, self.users[0], "Web", [21])
            await db.commit()
            self.assertEqual(assignment.assignment.challenge_ids, [21])

    async def _wait_blocked(self, waiter_pid, holder_pid):
        async def observe():
            async with self.engine.connect() as connection:
                while True:
                    blocked = (await connection.execute(text("SELECT :holder = ANY(pg_blocking_pids(:waiter))"),
                                                        {"holder": holder_pid, "waiter": waiter_pid})).scalar_one()
                    if blocked:
                        return
                    await asyncio.sleep(0.01)
        await asyncio.wait_for(observe(), 3)

    @asynccontextmanager
    async def _reject_inserts(self, table):
        self.assertIn(table, {"competition_team_members", "competition_team_solves"})
        async with self.engine.begin() as connection:
            await connection.execute(text(f'CREATE FUNCTION "{self.schema}".owned_insert_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION \'owned integration fault\' USING ERRCODE = \'23514\'; END; $$'))
            await connection.execute(text(f'CREATE TRIGGER owned_insert_failure BEFORE INSERT ON "{self.schema}"."{table}" FOR EACH ROW EXECUTE FUNCTION "{self.schema}".owned_insert_failure()'))
        try:
            yield
        finally:
            async with self.engine.begin() as connection:
                await connection.execute(text(f'DROP TRIGGER owned_insert_failure ON "{self.schema}"."{table}"'))
                await connection.execute(text(f'DROP FUNCTION "{self.schema}".owned_insert_failure()'))

    async def test_team_membership_lock_orders_registration_and_fixed_snapshot_is_immutable(self):
        ready = asyncio.Event()
        waiter = {}
        async def register():
            async with self.sessions() as db:
                waiter["pid"] = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                ready.set()
                row = await service.register_team(db, self.competition, self.team_a, self.users[0])
                await db.commit()
                return row
        async with self.sessions() as membership:
            holder = (await membership.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            team = await service.lock_team_for_membership(membership, self.team_a)
            team.name = "registration snapshot"
            await membership.execute(delete(TeamMember).where(TeamMember.team_id == self.team_a, TeamMember.user_id == self.users[1]))
            membership.add(TeamMember(team_id=self.team_a, user_id=self.users[3]))
            await membership.flush()
            task = asyncio.create_task(register())
            try:
                await asyncio.wait_for(ready.wait(), 3)
                await self._wait_blocked(waiter["pid"], holder)
                await membership.commit()
                first = await asyncio.wait_for(task, 10)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        async with self.sessions() as db:
            team = await service.lock_team_for_membership(db, self.team_a)
            team.name, team.captain_id = "changed later", self.users[2]
            await db.execute(delete(TeamMember).where(TeamMember.team_id == self.team_a, TeamMember.user_id == self.users[3]))
            (await db.get(User, self.users[0])).username = "changed username"
            await db.commit()
        async with self.sessions() as db:
            context = await service.resolve_team_context(db, self.competition, self.users[3])
            self.assertEqual(context.team_name, "registration snapshot")
            self.assertEqual({row.user_id for row in context.members}, {self.users[0], self.users[3]})
            self.assertEqual({row.user_id: row.username_snapshot for row in context.members}[self.users[0]], self.names[self.users[0]])
            replay = await service.register_team(db, self.competition, self.team_a, self.users[0])
            self.assertEqual(replay.registered_at, first.registered_at)
            await db.commit()
        self.assertEqual((await self._counts())["competition_team_registrations"], 1)

    async def test_parent_lock_serializes_overlapping_rosters_and_rejects_the_whole_second_registration(self):
        async with self.sessions() as db:
            db.add(TeamMember(team_id=self.team_b, user_id=self.users[1]))
            await db.commit()
        ready, waiter = asyncio.Event(), {}
        async def second_registration():
            async with self.sessions() as db:
                waiter["pid"] = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                ready.set()
                try:
                    await service.register_team(db, self.competition, self.team_b, self.users[2])
                except service.CompetitionTeamError as error:
                    await db.rollback()
                    return error.status_code, error.code
                await db.commit()
                return None
        async with self.sessions() as first:
            holder = (await first.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await service.register_team(first, self.competition, self.team_a, self.users[0])
            task = asyncio.create_task(second_registration())
            try:
                await asyncio.wait_for(ready.wait(), 3)
                await self._wait_blocked(waiter["pid"], holder)
                await first.commit()
                self.assertEqual(await asyncio.wait_for(task, 10), (409, "roster_overlap"))
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        async with self.sessions() as db:
            self.assertIsNone(await db.get(CompetitionTeamRegistration, (self.competition, self.team_b)))
            rows = (await db.execute(select(CompetitionTeamMember).where(CompetitionTeamMember.competition_id == self.competition))).scalars().all()
            self.assertEqual({(row.team_id, row.user_id) for row in rows}, {(self.team_a, self.users[0]), (self.team_a, self.users[1])})

    async def test_simultaneous_category_selection_returns_one_identical_shared_assignment(self):
        await self._register()
        barrier = asyncio.Barrier(2)
        async def assign(user_id):
            async with self.sessions() as db:
                pid = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                await barrier.wait()
                result = await service.get_or_create_team_assignment(db, self.competition, user_id, "Web", list(range(21, 28)))
                await db.commit()
                return pid, result.already_assigned, list(result.assignment.challenge_ids)
        rows = await asyncio.wait_for(asyncio.gather(assign(self.users[0]), assign(self.users[1])), 10)
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertEqual(sorted(row[1] for row in rows), [False, True])
        self.assertEqual(rows[0][2], rows[1][2])
        self.assertEqual(len(set(rows[0][2])), 5)
        self.assertTrue(set(rows[0][2]) <= set(range(21, 28)))
        self.assertEqual((await self._counts())["competition_team_assignments"], 1)

    async def test_two_concurrent_personal_acceptances_count_as_one_team_solve(self):
        await self._assign_one()
        barrier = asyncio.Barrier(2)
        async def solve(user_id):
            async with self.sessions() as db:
                await db.execute(text("SELECT pg_backend_pid()"))
                await barrier.wait()
                result = await service.record_team_solve(db, self.competition, user_id, 21, self.now)
                await db.commit()
                return result.personal_inserted, result.team_inserted
        rows = await asyncio.wait_for(asyncio.gather(solve(self.users[0]), solve(self.users[1])), 10)
        self.assertEqual(sorted(rows), [(True, False), (True, True)])
        counts = await self._counts()
        self.assertEqual(counts["competition_solves"], 2)
        self.assertEqual(counts["competition_team_solves"], 1)
        async with self.sessions() as db:
            self.assertEqual((await db.get(Team, self.team_a)).total_score, 23)
            self.assertEqual([(await db.get(User, user_id)).xp for user_id in self.users], [17] * 4)

    async def test_earlier_accepted_at_corrects_the_representative_without_an_extra_solve(self):
        await self._assign_one()
        async with self.sessions() as db:
            await service.record_team_solve(db, self.competition, self.users[0], 21, self.now + timedelta(seconds=20))
            await db.commit()
        async with self.sessions() as db:
            result = await service.record_team_solve(db, self.competition, self.users[1], 21, self.now)
            self.assertTrue(result.personal_inserted)
            self.assertFalse(result.team_inserted)
            self.assertTrue(result.earliest_changed)
            await db.commit()
        async with self.sessions() as db:
            replay = await service.record_team_solve(db, self.competition, self.users[1], 21, self.now)
            self.assertEqual((replay.personal_inserted, replay.team_inserted, replay.earliest_changed), (False, False, False))
            await db.commit()
            solve = await db.get(CompetitionTeamSolve, (self.competition, self.team_a, 21))
            self.assertEqual((solve.first_solver_id, solve.solved_at), (self.users[1], self.now))
        counts = await self._counts()
        self.assertEqual((counts["competition_solves"], counts["competition_team_solves"]), (2, 1))

    async def test_registration_flush_failure_rolls_back_the_parent_and_entire_roster(self):
        async with self._reject_inserts("competition_team_members"):
            async with self.sessions() as db:
                with self.assertRaises(service.CompetitionTeamError) as error:
                    await service.register_team(db, self.competition, self.team_a, self.users[0])
                self.assertEqual(error.exception.status_code, 409)
                await db.rollback()
        counts = await self._counts()
        self.assertEqual((counts["competition_team_registrations"], counts["competition_team_members"]), (0, 0))
        await self._register()
        self.assertEqual((await self._counts())["competition_team_members"], 2)

    async def test_team_solve_flush_failure_cannot_commit_a_personal_only_result(self):
        await self._assign_one()
        async with self._reject_inserts("competition_team_solves"):
            async with self.sessions() as db:
                with self.assertRaises(DBAPIError):
                    await service.record_team_solve(db, self.competition, self.users[0], 21, self.now)
                await db.rollback()
        counts = await self._counts()
        self.assertEqual((counts["competition_solves"], counts["competition_team_solves"]), (0, 0))
        async with self.sessions() as db:
            result = await service.record_team_solve(db, self.competition, self.users[0], 21, self.now)
            self.assertTrue(result.personal_inserted and result.team_inserted)
            await db.commit()

    async def test_deleting_only_the_synthetic_live_team_preserves_all_competition_ledgers(self):
        await self._assign_one()
        async with self.sessions() as db:
            await service.record_team_solve(db, self.competition, self.users[0], 21, self.now)
            await db.commit()
        before = await self._counts()
        async with self.sessions() as db:
            await db.execute(delete(Team).where(Team.id == self.team_a))
            await db.commit()
        self.assertEqual(await self._counts(), before)
        async with self.sessions() as db:
            self.assertIsNone(await db.get(Team, self.team_a))
            self.assertEqual((await db.execute(select(func.count()).select_from(TeamMember).where(TeamMember.team_id == self.team_a))).scalar_one(), 0)
            context = await service.resolve_team_context(db, self.competition, self.users[1])
            self.assertEqual(context.team_name, self.team_name)
            self.assertEqual({member.user_id for member in context.members}, {self.users[0], self.users[1]})
        self.assertEqual(await self._legacy_snapshot(), self.legacy_before)

    async def test_actual_006_triggers_allow_null_contract_and_reject_legacy_participant_assignment_writes(self):
        async with self.sessions() as db:
            self.assertIsNone((await db.get(Competition, self.legacy)).team_contract)
            db.add(CompetitionParticipant(competition_id=self.legacy, user_id=self.users[1]))
            db.add(CompetitionAssignment(competition_id=self.legacy, user_id=self.users[1], category="Web", challenge_ids=[21]))
            await db.commit()
        for model in (CompetitionParticipant, CompetitionAssignment):
            async with self.sessions() as db:
                values = {"competition_id": self.competition, "user_id": self.users[1]}
                if model is CompetitionAssignment:
                    values.update(category="Web", challenge_ids=[21])
                db.add(model(**values))
                with self.assertRaises(IntegrityError) as error:
                    await db.flush()
                self.assertEqual(error.exception.orig.sqlstate, "23514")
                await db.rollback()
            async with self.sessions() as db:
                row = await db.get(model, (self.legacy, self.users[1], "Web") if model is CompetitionAssignment else (self.legacy, self.users[1]))
                row.competition_id = self.competition
                with self.assertRaises(IntegrityError) as error:
                    await db.flush()
                self.assertEqual(error.exception.orig.sqlstate, "23514")
                await db.rollback()
        async with self.sessions() as db:
            for model in (CompetitionParticipant, CompetitionAssignment):
                self.assertEqual((await db.execute(select(func.count()).select_from(model).where(model.competition_id == self.competition))).scalar_one(), 0)

    async def test_actual_006_downgrade_refuses_fixed_contract_without_removing_ddl_or_rows(self):
        await self._assign_one()
        before, legacy = await self._counts(), await self._legacy_snapshot()
        with self.assertRaisesRegex(RuntimeError, "downgrade"):
            await self._migrate("downgrade")
        self.assertEqual(await self._counts(), before)
        self.assertEqual(await self._legacy_snapshot(), legacy)
        async with self.sessions() as db:
            self.assertEqual((await db.get(Competition, self.competition)).team_contract, service.FIXED_ROSTER_CONTRACT)
            self.assertEqual((await db.execute(text("SELECT count(*) FROM information_schema.columns WHERE table_schema = :schema AND table_name = 'competitions' AND column_name = 'team_contract'"), {"schema": self.schema})).scalar_one(), 1)


class CompetitionTeamDatabaseUrlTests(unittest.TestCase):
    work_name = "ctfi_a_e7ab280d5d0e4a2c89aab3650d03ff12"
    work_url = f"postgresql+asyncpg://{work_name}:owned%40test@127.0.0.1:15433/{work_name}"

    def invalid_urls(self):
        return (
            self.work_url.replace(self.work_name, "ctf_acceptance_auth"),
            self.work_url.rsplit("/", 1)[0] + "/ctf_acceptance_auth",
            self.work_url.replace(f"//{self.work_name}:", "//ctfi_a_" + "a" * 32 + ":"),
            self.work_url.replace(":15433/", ":5432/"),
            self.work_url.replace(":15433/", "/"),
            self.work_url.replace("127.0.0.1", "localhost"),
            self.work_url.replace("127.0.0.1", "[::1]"),
            self.work_url.replace("127.0.0.1", "192.0.2.1"),
            self.work_url + "?sslmode=disable",
            self.work_url + "?options=-csearch_path=public",
            self.work_url + "?",
            self.work_url + "#fragment",
            self.work_url.replace("postgresql+asyncpg", "postgresql"),
            self.work_url.replace(self.work_name, self.work_name.upper()),
            self.work_url.replace(":owned%40test@", "@"),
            self.work_url.replace(":owned%40test@", ":@"),
            True, False, None, 15433, "", "not-a-database-url",
        )

    def test_exact_owned_work_url_is_accepted_without_connecting(self):
        with patch(__name__ + ".create_async_engine") as engine:
            url = validate_work_database_url(self.work_url)
        self.assertEqual((url.database, url.username), (self.work_name, self.work_name))
        self.assertEqual((url.host, url.port, url.password), ("127.0.0.1", 15433, "owned@test"))
        self.assertEqual(dict(url.query), {})
        engine.assert_not_called()

    def test_original_unknown_or_malformed_url_is_rejected_without_connecting(self):
        for value in self.invalid_urls():
            with self.subTest(value_type=type(value).__name__), patch(__name__ + ".create_async_engine") as engine:
                with self.assertRaisesRegex(ValueError, "작업 소유 ctfi_a"):
                    validate_work_database_url(value)
                engine.assert_not_called()

    def test_async_setup_rejects_invalid_urls_before_engine_creation(self):
        for value in self.invalid_urls():
            with self.subTest(value_type=type(value).__name__), patch(__name__ + ".TEST_URL", value), \
                    patch(__name__ + ".create_async_engine") as engine:
                case = PostgreSQLCompetitionTeamTests()
                setup = case.asyncSetUp()
                try:
                    with self.assertRaises(ValueError):
                        setup.send(None)
                finally:
                    setup.close()
                engine.assert_not_called()

    def test_connected_database_and_role_are_checked_before_schema_creation(self):
        for database, role, version in (
            ("ctf_acceptance_auth", self.work_name, 160000),
            (self.work_name, "other_role", 160000),
            (self.work_name, self.work_name, 150000),
        ):
            with self.subTest(database_matches=database == self.work_name, role_matches=role == self.work_name):
                result = MagicMock()
                result.one.return_value = (database, role, version)
                connection = MagicMock()
                connection.execute = AsyncMock(return_value=result)
                engine = MagicMock()
                engine.begin.return_value.__aenter__ = AsyncMock(return_value=connection)
                engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)
                with patch(__name__ + ".TEST_URL", self.work_url), \
                        patch(__name__ + ".create_async_engine", return_value=engine) as create_engine:
                    case = PostgreSQLCompetitionTeamTests()
                    setup = case.asyncSetUp()
                    try:
                        with self.assertRaises(AssertionError):
                            setup.send(None)
                    finally:
                        setup.close()
                create_engine.assert_called_once()
                self.assertEqual(connection.execute.await_count, 1)
                self.assertTrue(str(connection.execute.call_args.args[0]).startswith("SELECT current_database(),current_user,"))
                self.assertFalse(case.created_schema)


if __name__ == "__main__":
    unittest.main()
