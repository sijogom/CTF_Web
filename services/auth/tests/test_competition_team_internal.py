"""팀 풀이의 단일 caller commit과 팀 최초 풀이 알림 경계."""

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.api import internal
from app.models.notification import Notification


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class _Session:
    def __init__(self, results=(), *, commit_error=None):
        self.results = list(results)
        self.statements = []
        self.added = []
        self.commits = 0
        self.commit_error = commit_error

    async def execute(self, statement):
        self.statements.append(statement)
        if not self.results:
            raise AssertionError("UNEXPECTED_DATABASE_CALL")
        return _Result(self.results.pop(0))

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error


class CompetitionTeamInternalTests(unittest.IsolatedAsyncioTestCase):
    def request(self):
        return internal.CompetitionSolveRequest(
            competition_id="00000000-0000-4000-8000-000000000001",
            user_id="00000000-0000-4000-8000-000000000003", challenge_id=7,
            accepted_at=datetime(2026, 10, 18, 12, tzinfo=timezone.utc),
        )

    def result(self, **changes):
        values = dict(team_id="00000000-0000-4000-8000-000000000002", personal_inserted=True,
                      team_inserted=True, earliest_changed=False,
                      solve=SimpleNamespace(first_solver_id=self.request().user_id))
        values.update(changes)
        return SimpleNamespace(**values)

    async def invoke(self, session, result):
        comp = SimpleNamespace(mode="team", team_contract="fixed_roster_v1", title='A & B "Q" <FLAG>')
        with patch.object(internal, "require_competition_challenge_access", new=AsyncMock(return_value=comp)), \
             patch.object(internal.team_service, "record_team_solve", new=AsyncMock(return_value=result)) as record:
            response = await internal.record_competition_solve(self.request(), db=session)
        record.assert_awaited_once()
        self.assertEqual(record.await_args.args[0], session)
        return response

    async def test_first_team_row_and_notification_share_one_caller_commit(self):
        session = _Session([1, "solver", 'A & B "Q" <FLAG>'])
        self.assertEqual(await self.invoke(session, self.result()), {"status": "recorded"})
        self.assertEqual(session.commits, 1)
        notifications = [item for item in session.added if isinstance(item, Notification)]
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].content, '<p>A &amp; B "Q" &lt;FLAG&gt; — Challenge #7</p>')
        count_queries = " ".join(str(statement) for statement in session.statements)
        self.assertIn("competition_team_solves", count_queries)
        self.assertNotIn("competition_participants", count_queries)

    async def test_second_team_does_not_create_another_first_blood(self):
        session = _Session([2])
        self.assertEqual(await self.invoke(session, self.result()), {"status": "recorded"})
        self.assertEqual(session.commits, 1)
        self.assertEqual(session.added, [])

    async def test_another_member_personal_solve_does_not_repeat_team_notification(self):
        session = _Session()
        result = self.result(team_inserted=False)
        self.assertEqual(await self.invoke(session, result), {"status": "recorded"})
        self.assertEqual(session.commits, 1)
        self.assertEqual(session.added, [])
        self.assertEqual(session.statements, [])

    async def test_duplicate_and_earlier_team_correction_never_repeat_notification(self):
        for earlier in (False, True):
            session = _Session()
            result = self.result(personal_inserted=False, team_inserted=False, earliest_changed=earlier)
            with self.subTest(earlier=earlier):
                self.assertEqual(await self.invoke(session, result), {"status": "already_recorded"})
            self.assertEqual(session.commits, 1)
            self.assertEqual(session.added, [])
            self.assertEqual(session.statements, [])

    async def test_commit_failure_is_not_reported_as_recorded(self):
        session = _Session([1, "solver", 'A & B "Q" <FLAG>'], commit_error=RuntimeError("synthetic-commit-failure"))
        with self.assertRaisesRegex(RuntimeError, "synthetic-commit-failure"):
            await self.invoke(session, self.result())
        self.assertEqual(session.commits, 1)


if __name__ == "__main__":
    unittest.main()
