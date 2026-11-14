import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, Table, create_engine

from app.api import stats


class PlatformStatsTests(unittest.IsolatedAsyncioTestCase):
    """실제 집계 쿼리를 격리된 메모리 DB에 실행한다. 운영 DB에는 연결하지 않는다."""

    def setUp(self):
        self.now = datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)
        self.engine = create_engine("sqlite:///:memory:")
        metadata = MetaData()
        self.users = Table("users", metadata, Column("id", Integer, primary_key=True), Column("is_active", Boolean))
        self.competitions = Table(
            "competitions", metadata, Column("id", Integer, primary_key=True),
            Column("start_at", DateTime), Column("end_at", DateTime),
            Column("is_published", Boolean), Column("is_deleted", Boolean),
        )
        metadata.create_all(self.engine)
        self.connection = self.engine.connect()
        self.connection.execute(self.users.insert(), [{"is_active": True}, {"is_active": False}])
        self.db = MagicMock()
        self.db.execute = AsyncMock(side_effect=self.connection.execute)
        self.client = AsyncMock()
        self.client.get.return_value = MagicMock(
            status_code=200, json=MagicMock(return_value={"total_challenges": 6, "total_solves": 5}),
        )

    def tearDown(self):
        self.connection.close()
        self.engine.dispose()

    async def read_stats(self):
        with patch.object(stats, "datetime") as clock, patch.object(stats.httpx, "AsyncClient") as client:
            clock.now.return_value = self.now
            client.return_value.__aenter__ = AsyncMock(return_value=self.client)
            client.return_value.__aexit__ = AsyncMock(return_value=False)
            return await stats.platform_stats(self.db)

    async def test_counts_only_published_active_undeleted_competitions(self):
        cases = [
            ("진행 중 공개", -1, 1, True, False, 1),
            ("시작 시각 포함", 0, 1, True, False, 1),
            ("종료 시각 제외", -1, 0, True, False, 0),
            ("종료 대회", -2, -1, True, False, 0),
            ("예정 대회", 1, 2, True, False, 0),
            ("비공개 대회", -1, 1, False, False, 0),
            ("삭제된 대회", -1, 1, True, True, 0),
        ]
        for name, start, end, published, deleted, expected in cases:
            with self.subTest(name=name):
                self.connection.execute(self.competitions.delete())
                self.connection.execute(self.competitions.insert().values(
                    start_at=self.now + timedelta(hours=start), end_at=self.now + timedelta(hours=end),
                    is_published=published, is_deleted=deleted,
                ))
                result = await self.read_stats()
                self.assertEqual(result, {"users": 1, "competitions": expected, "challenges": 6, "solves": 5})

    async def test_no_competitions_returns_zero(self):
        self.assertEqual((await self.read_stats())["competitions"], 0)

    async def test_learn_failure_preserves_auth_counts(self):
        self.connection.execute(self.competitions.insert().values(
            start_at=self.now - timedelta(hours=1), end_at=self.now + timedelta(hours=1),
            is_published=True, is_deleted=False,
        ))
        self.client.get.side_effect = RuntimeError("isolated Learn unavailable")
        result = await self.read_stats()
        self.assertEqual(result, {"users": 1, "competitions": 1, "challenges": 0, "solves": 0})
