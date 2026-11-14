"""장터 계약 단위 검증과 지정된 로컬 PG의 실제 원자성·동시성 검증."""

import asyncio
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.point_transfer import MarketplacePointTransfer
from app.models.user import User
from app.schemas.point_transfer import MarketplaceTransferRequest, marketplace_transfer_digest
from app.utils.point_transfers import settle_marketplace_transfer


def request(buyer_id, seller_id, *, price=100, operation_id=None):
    earned = max(1, price * 20 // 100)
    return MarketplaceTransferRequest(
        operation_id=operation_id or uuid4(), buyer_id=buyer_id, seller_id=seller_id,
        price_paid=price, seller_earned=earned,
        payload_digest=marketplace_transfer_digest(buyer_id, seller_id, price, earned),
    )


class TransferContractTests(unittest.TestCase):
    def test_mutated_digest_commission_and_boolean_amount_are_rejected(self):
        valid = request(uuid4(), uuid4()).model_dump()
        for change in [{"payload_digest": "0" * 64}, {"seller_earned": 99}, {"price_paid": True}]:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                MarketplaceTransferRequest(**(valid | change))

    def test_one_point_price_preserves_existing_minimum_seller_payment(self):
        value = request(uuid4(), uuid4(), price=1)
        self.assertEqual(value.seller_earned, 1)


DATABASE_URL = os.environ.get("MARKETPLACE_TEST_DATABASE_URL")


@unittest.skipUnless(DATABASE_URL, "MARKETPLACE_TEST_DATABASE_URL로 작업 소유 로컬 PG를 지정해야 합니다.")
class TransferPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if make_url(DATABASE_URL).host not in {"127.0.0.1", "localhost", "::1"}:
            self.fail("이 검증은 지정된 로컬 PG에만 실행합니다.")
        self.schema = "commerce_auth_" + uuid4().hex
        self.control = create_async_engine(DATABASE_URL)
        async with self.control.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_async_engine(DATABASE_URL, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(User.__table__.create)
        self.buyer_id, self.seller_id = uuid4(), uuid4()
        async with self.sessions() as db:
            db.add_all([
                User(id=self.buyer_id, email="buyer@example.invalid", username="buyer", points=1000),
                User(id=self.seller_id, email="seller@example.invalid", username="seller", points=0),
            ])
            await db.commit()
        path = Path(__file__).resolve().parents[1] / "migrations/versions/20261013_004_marketplace_point_transfers.py"
        spec = importlib.util.spec_from_file_location("marketplace_auth_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        async with self.engine.begin() as connection:
            def upgrade(sync_connection):
                with Operations.context(MigrationContext.configure(sync_connection)):
                    migration.upgrade()
            await connection.run_sync(upgrade)

    async def asyncTearDown(self):
        await self.engine.dispose()
        async with self.control.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        await self.control.dispose()

    async def settle(self, body):
        async with self.sessions() as db:
            return await settle_marketplace_transfer(db, body)

    async def balances(self):
        async with self.sessions() as db:
            users = (await db.execute(select(User))).scalars().all()
            return {user.id: user.points for user in users}

    async def test_concurrent_same_id_debits_and_credits_once(self):
        body = request(self.buyer_id, self.seller_id)
        outcomes = await asyncio.gather(*(self.settle(body) for _ in range(8)))
        self.assertEqual([outcome.state for outcome in outcomes], ["completed"] * 8)
        self.assertEqual(await self.balances(), {self.buyer_id: 900, self.seller_id: 20})
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(MarketplacePointTransfer)), 1)

    async def test_same_id_with_changed_payload_is_409_without_second_transfer(self):
        first = request(self.buyer_id, self.seller_id)
        await self.settle(first)
        with self.assertRaises(HTTPException) as caught:
            await self.settle(request(self.buyer_id, self.seller_id, price=200, operation_id=first.operation_id))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(await self.balances(), {self.buyer_id: 900, self.seller_id: 20})

    async def test_competing_operations_cannot_overdraw_buyer(self):
        async with self.sessions() as db:
            buyer = await db.get(User, self.buyer_id)
            buyer.points = 150
            await db.commit()
        outcomes = await asyncio.gather(*(self.settle(request(self.buyer_id, self.seller_id)) for _ in range(2)))
        self.assertCountEqual([outcome.state for outcome in outcomes], ["completed", "rejected"])
        self.assertEqual(await self.balances(), {self.buyer_id: 50, self.seller_id: 20})

    async def test_rejected_operation_is_terminal_even_after_balance_changes(self):
        body = request(self.seller_id, self.buyer_id)
        first = await self.settle(body)
        self.assertEqual(first.failure_code, "insufficient_points")
        async with self.sessions() as db:
            seller = await db.get(User, self.seller_id)
            seller.points = 500
            await db.commit()
        self.assertEqual((await self.settle(body)).state, "rejected")
        self.assertEqual(await self.balances(), {self.buyer_id: 1000, self.seller_id: 500})

    async def test_opposite_direction_transfers_use_consistent_lock_order(self):
        async with self.sessions() as db:
            seller = await db.get(User, self.seller_id)
            seller.points = 1000
            await db.commit()
        await asyncio.wait_for(asyncio.gather(
            self.settle(request(self.buyer_id, self.seller_id)),
            self.settle(request(self.seller_id, self.buyer_id)),
        ), timeout=10)
        self.assertEqual(await self.balances(), {self.buyer_id: 920, self.seller_id: 920})

    async def test_missing_seller_never_debits_buyer(self):
        outcome = await self.settle(request(self.buyer_id, uuid4()))
        self.assertEqual(outcome.failure_code, "account_unavailable")
        self.assertEqual((await self.balances())[self.buyer_id], 1000)

    async def test_database_failure_rolls_back_debit_credit_and_ledger(self):
        async with self.sessions() as db:
            seller = await db.get(User, self.seller_id)
            seller.points = 2147483647
            await db.commit()
        with self.assertRaises(Exception):
            await self.settle(request(self.buyer_id, self.seller_id))
        self.assertEqual(await self.balances(), {self.buyer_id: 1000, self.seller_id: 2147483647})
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(MarketplacePointTransfer)), 0)

    async def test_additive_migration_preserves_existing_balances(self):
        self.assertEqual(await self.balances(), {self.buyer_id: 1000, self.seller_id: 0})

    async def test_internal_api_requires_key_and_replays_confirmed_result(self):
        from app.api import internal
        from app.database import get_db
        app = FastAPI()
        app.include_router(internal.router, prefix="/internal")
        async def test_db():
            async with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = test_db
        body = request(self.buyer_id, self.seller_id)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid") as client:
            with patch.object(internal.settings, "internal_api_key", "commerce-test-key"):
                denied = await client.post("/internal/points/transfers", json=body.model_dump(mode="json"))
                self.assertEqual(denied.status_code, 403)
                headers = {"X-Internal-Key": "commerce-test-key"}
                accepted = await client.post("/internal/points/transfers", headers=headers, json=body.model_dump(mode="json"))
                self.assertEqual(accepted.status_code, 200)
                replay = await client.post("/internal/points/transfers", headers=headers, json=body.model_dump(mode="json"))
                self.assertEqual(replay.json(), accepted.json())
                status = await client.get(f"/internal/points/transfers/{body.operation_id}", headers=headers)
                self.assertEqual(status.json(), accepted.json())
                missing = await client.get(f"/internal/points/transfers/{uuid4()}", headers=headers)
                self.assertEqual(missing.status_code, 404)
        self.assertEqual(await self.balances(), {self.buyer_id: 900, self.seller_id: 20})


if __name__ == "__main__":
    unittest.main(verbosity=2)
