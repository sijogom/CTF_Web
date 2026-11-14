from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, patch

from app.api import internal
from app.api.internal import CompetitionSolveRequest
from app.api.notification import NotificationCreate
from app.models.notification import Notification


class _Result:
    def __init__(self, *, rowcount: int | None = None, scalar_value=None) -> None:
        self.rowcount = rowcount
        self._scalar_value = scalar_value

    def scalar(self):
        return self._scalar_value


class _FakeSession:
    def __init__(self, results: list[_Result]) -> None:
        self._results = list(results)
        self.added: list[object] = []
        self.commit_count = 0

    async def execute(self, _statement):
        if not self._results:
            raise AssertionError("unexpected database execute")
        return self._results.pop(0)

    async def commit(self) -> None:
        self.commit_count += 1

    def add(self, value: object) -> None:
        self.added.append(value)


class NotificationRichContentTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_blood_escapes_plain_title_and_preserves_solve_flow(self) -> None:
        session = _FakeSession([
            _Result(rowcount=1),
            _Result(scalar_value=1),
            _Result(scalar_value="solver"),
            _Result(scalar_value='A & B "Q" <FLAG>'),
        ])
        request = CompetitionSolveRequest(
            competition_id="00000000-0000-0000-0000-000000000001",
            user_id="00000000-0000-0000-0000-000000000002",
            challenge_id=7,
            accepted_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
        )

        with patch.object(
            internal,
            "require_competition_challenge_access",
            new=AsyncMock(return_value=None),
        ) as access_check:
            result = await internal.record_competition_solve(request, db=session)

        self.assertEqual(result, {"status": "recorded"})
        self.assertEqual(session.commit_count, 2)
        access_check.assert_awaited_once()
        notifications = [
            value for value in session.added if isinstance(value, Notification)
        ]
        self.assertEqual(len(notifications), 1)
        self.assertEqual(
            notifications[0].content,
            '<p>A &amp; B "Q" &lt;FLAG&gt; — Challenge #7</p>',
        )

    async def test_duplicate_solve_does_not_create_another_notification(self) -> None:
        session = _FakeSession([_Result(rowcount=0)])
        request = CompetitionSolveRequest(
            competition_id="00000000-0000-0000-0000-000000000001",
            user_id="00000000-0000-0000-0000-000000000002",
            challenge_id=7,
            accepted_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
        )
        with patch.object(
            internal,
            "require_competition_challenge_access",
            new=AsyncMock(return_value=None),
        ):
            result = await internal.record_competition_solve(request, db=session)
        self.assertEqual(result, {"status": "already_recorded"})
        self.assertEqual(session.commit_count, 1)
        self.assertEqual(session.added, [])

    def test_notification_title_remains_plain_while_content_is_canonical_html(self) -> None:
        payload = NotificationCreate(
            title="<FLAG>",
            content='A & B "Q"',
            type="toast",
        )
        self.assertEqual(payload.title, "<FLAG>")
        self.assertEqual(payload.content, '<p>A &amp; B "Q"</p>')


if __name__ == "__main__":
    unittest.main()
