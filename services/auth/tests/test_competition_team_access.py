"""고정 명단·배정 기반 내부 접근과 accepted_at 시간 경계의 회귀."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import HTTPException

from app.utils import competition_access as access


NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
COMP_ID = UUID("00000000-0000-4000-8000-000000000001")
USER_ID = UUID("00000000-0000-4000-8000-000000000003")
TEAM_ID = UUID("00000000-0000-4000-8000-000000000002")


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value


class _Session:
    def __init__(self, rows):
        self.rows = list(rows)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if not self.rows:
            raise AssertionError("UNEXPECTED_DATABASE_CALL")
        return _Result(self.rows.pop(0))


def fixed_comp(**changes):
    value = dict(id=COMP_ID, mode="team", team_contract="fixed_roster_v1", is_deleted=False, is_published=True,
                 start_at=NOW - timedelta(hours=1), end_at=NOW + timedelta(hours=1))
    value.update(changes)
    return SimpleNamespace(**value)


def context():
    return SimpleNamespace(
        registration=SimpleNamespace(competition_id=COMP_ID, team_id=TEAM_ID),
        member=SimpleNamespace(user_id=USER_ID), members=(), team_id=TEAM_ID,
    )


class CompetitionTeamAccessTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, session, **values):
        with patch.object(access, "datetime", _Clock):
            return await access.require_competition_challenge_access(
                competition_id=str(COMP_ID), user_id=str(USER_ID), challenge_id=7,
                db=session, **values,
            )

    async def test_fixed_team_access_uses_user_roster_and_team_assignment_not_personal_mirrors(self):
        comp = fixed_comp()
        session = _Session([comp, SimpleNamespace(challenge_id=7), [[7, 8]]])
        with patch.object(access.team_service, "resolve_team_context", new=AsyncMock(return_value=context())) as resolve:
            result = await self.invoke(session)
        self.assertIs(result, comp)
        resolve.assert_awaited_once()
        self.assertEqual(resolve.await_args.args[:3], (session, COMP_ID, USER_ID))
        queries = " ".join(str(statement) for statement in session.statements)
        self.assertIn("competition_team_assignments", queries)
        self.assertNotIn("competition_participants", queries)
        self.assertNotIn("competition_assignments", queries.replace("competition_team_assignments", ""))

    async def test_live_global_team_membership_cannot_replace_fixed_roster(self):
        session = _Session([fixed_comp(), SimpleNamespace(challenge_id=7), [[7]]])
        with patch.object(access.team_service, "resolve_team_context", new=AsyncMock(return_value=None)):
            with self.assertRaises(HTTPException) as caught:
                await self.invoke(session)
        self.assertEqual(caught.exception.status_code, 403)

    async def test_other_team_assignment_does_not_grant_current_user_access(self):
        session = _Session([fixed_comp(), SimpleNamespace(challenge_id=7), [[8]]])
        with patch.object(access.team_service, "resolve_team_context", new=AsyncMock(return_value=context())):
            with self.assertRaises(HTTPException) as caught:
                await self.invoke(session)
        self.assertEqual(caught.exception.status_code, 403)

    async def test_unpublished_deleted_and_outside_period_fail_before_roster_lookup(self):
        for comp, status in ((fixed_comp(is_published=False), 404), (fixed_comp(is_deleted=True), 404),
                             (fixed_comp(start_at=NOW + timedelta(seconds=1)), 403), (fixed_comp(end_at=NOW), 403)):
            with self.subTest(comp=comp), patch.object(access.team_service, "resolve_team_context", new=AsyncMock()) as resolve:
                with self.assertRaises(HTTPException) as caught:
                    await self.invoke(_Session([comp]))
                self.assertEqual(caught.exception.status_code, status)
                resolve.assert_not_awaited()

    async def test_learn_accepted_timestamp_preserves_delayed_reconciliation_after_end(self):
        comp = fixed_comp(end_at=NOW - timedelta(minutes=1))
        accepted_at = comp.end_at - timedelta(microseconds=1)
        session = _Session([comp, comp, SimpleNamespace(challenge_id=7), [[7]]])
        with patch.object(access.team_service, "resolve_team_context", new=AsyncMock(return_value=context())):
            self.assertIs(await self.invoke(session, accepted_at=accepted_at), comp)

    async def test_accepted_timestamp_is_aware_and_at_most_sixty_seconds_ahead(self):
        for accepted_at in (NOW.replace(tzinfo=None), NOW + timedelta(seconds=60, microseconds=1)):
            with self.subTest(accepted_at=accepted_at), patch.object(access.team_service, "resolve_team_context", new=AsyncMock()) as resolve:
                with self.assertRaises(HTTPException) as caught:
                    await self.invoke(_Session([fixed_comp(), fixed_comp()]), accepted_at=accepted_at)
                self.assertEqual(caught.exception.status_code, 422)
                resolve.assert_not_awaited()

    async def test_solve_rechecks_publication_on_fresh_locked_parent_before_roster(self):
        session = _Session([fixed_comp(), fixed_comp(is_published=False)])
        with patch.object(access.team_service, "resolve_team_context", new=AsyncMock()) as resolve:
            with self.assertRaises(HTTPException) as caught:
                await self.invoke(session, accepted_at=NOW)
        self.assertEqual(caught.exception.status_code, 404)
        resolve.assert_not_awaited()
        self.assertIsNotNone(session.statements[1]._for_update_arg)
        self.assertTrue(session.statements[1].get_execution_options()["populate_existing"])

    async def test_nullable_legacy_team_preserves_personal_participant_assignment_path(self):
        comp = fixed_comp(team_contract=None)
        session = _Session([comp, SimpleNamespace(user_id=USER_ID), SimpleNamespace(challenge_id=7), [[7]]])
        with patch.object(access.team_service, "resolve_team_context", new=AsyncMock()) as resolve:
            self.assertIs(await self.invoke(session), comp)
        resolve.assert_not_awaited()
        queries = " ".join(str(statement) for statement in session.statements)
        self.assertIn("competition_participants", queries)
        self.assertNotIn("competition_team_assignments", queries)


if __name__ == "__main__":
    unittest.main()
