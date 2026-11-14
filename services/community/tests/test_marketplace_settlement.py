"""실제 PG 구매/권한 transaction과 격리 HTTP 응답 유실·lease 인계를 검증한다."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.marketplace import MarketplaceItem, MarketplacePurchase
from app.models.marketplace_settlement import MarketplacePurchaseOperation, MarketplaceSettlementOutbox
from app.utils.marketplace_settlement import (
    TransferGateway, assert_no_pending_purchase, claim_settlements, prepare_purchase,
    purchase_status, reconcile_operation, transfer_digest, utc_now,
)


class AuthTransport:
    """원격 commit 이후 timeout을 주입하는 Auth 계약 transport. 실제 Auth는 별도 PG 검증한다."""

    def __init__(self):
        self.ledger = {}
        self.posts = 0
        self.requests = 0
        self.lose_reply = False
        self.reject = False
        self.mismatch = False

    async def __call__(self, request):
        self.requests += 1
        if request.method == "GET":
            operation_id = request.url.path.rsplit("/", 1)[-1]
            data = self.ledger.get(operation_id)
            return httpx.Response(200, json=data) if data else httpx.Response(404)
        self.posts += 1
        payload = json.loads(request.content)
        data = {key: payload[key] for key in ("operation_id", "payload_digest", "price_paid", "seller_earned")}
        data.update(state="rejected" if self.reject else "completed", failure_code="insufficient_points" if self.reject else None)
        if self.mismatch:
            data["payload_digest"] = "0" * 64
        self.ledger[payload["operation_id"]] = data
        if self.lose_reply:
            self.lose_reply = False
            raise httpx.ReadTimeout("injected reply loss", request=request)
        return httpx.Response(200, json=data)


class TransferGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_digest_and_response_identity_are_verified_before_grant(self):
        operation = MarketplacePurchaseOperation(
            id=uuid4(), buyer_id=uuid4(), seller_id=uuid4(), price_paid=100, seller_earned=20,
            payload_digest="a" * 64,
        )
        transport = AuthTransport()
        transport.mismatch = True
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            result = await TransferGateway(client, "http://auth.invalid").confirm(operation)
        self.assertEqual(result.state, "blocked")

    async def test_transport_failure_is_unknown_not_rejected(self):
        operation = MarketplacePurchaseOperation(id=uuid4(), buyer_id=uuid4(), seller_id=uuid4(), price_paid=100, seller_earned=20, payload_digest="a" * 64)
        async def unavailable(request):
            raise httpx.ConnectError("injected", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
            self.assertEqual((await TransferGateway(client, "http://auth.invalid").confirm(operation)).state, "unknown")


DATABASE_URL = os.environ.get("MARKETPLACE_TEST_DATABASE_URL")


@unittest.skipUnless(DATABASE_URL, "MARKETPLACE_TEST_DATABASE_URL로 작업 소유 로컬 PG를 지정해야 합니다.")
class SettlementPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if make_url(DATABASE_URL).host not in {"127.0.0.1", "localhost", "::1"}:
            self.fail("이 검증은 지정된 로컬 PG에만 실행합니다.")
        self.schema = "commerce_community_" + uuid4().hex
        self.control = create_async_engine(DATABASE_URL)
        async with self.control.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_async_engine(DATABASE_URL, connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(MarketplaceItem.__table__.create)
            await connection.run_sync(MarketplacePurchase.__table__.create)
        path = Path(__file__).resolve().parents[1] / "migrations/versions/20261013_004_marketplace_settlement.py"
        spec = importlib.util.spec_from_file_location("marketplace_community_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        async with self.engine.begin() as connection:
            def upgrade(sync_connection):
                with Operations.context(MigrationContext.configure(sync_connection)):
                    migration.upgrade()
            await connection.run_sync(upgrade)
        self.item_id, self.buyer_id, self.seller_id = uuid4(), uuid4(), uuid4()
        async with self.sessions() as db:
            db.add(MarketplaceItem(id=self.item_id, title="fixture", price=100, seller_id=self.seller_id, seller_name="seller"))
            await db.commit()
        self.transport = AuthTransport()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.transport))
        self.gateway = TransferGateway(self.client, "http://auth.invalid")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()
        async with self.control.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        await self.control.dispose()

    async def prepare(self):
        async with self.sessions() as db:
            return await prepare_purchase(db, self.item_id, self.buyer_id)

    async def reconcile(self, operation_id, **options):
        async with self.sessions() as db:
            return await reconcile_operation(db, UUID(str(operation_id)), self.gateway, **options)

    async def assert_grants(self, count):
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(MarketplacePurchase)), count)
            self.assertEqual((await db.get(MarketplaceItem, self.item_id)).download_count, count)

    def visibility_api(self):
        """실제 router/PG에 인증 문맥만 주입한다. 원격 Auth/파일 저장소는 별도로 mock한다."""
        from app.api import marketplace
        from app.api.deps import get_current_user_id, get_optional_user_role
        from app.database import get_db
        app = FastAPI()
        app.include_router(marketplace.router)
        identity = {"id": self.buyer_id, "role": "student"}
        async def test_db():
            async with self.sessions() as db:
                yield db
        async def current_user():
            if identity["id"] is None:
                raise HTTPException(status_code=401, detail="인증이 필요합니다.")
            return identity["id"]
        async def optional_role():
            return identity["role"]
        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[get_current_user_id] = current_user
        app.dependency_overrides[get_optional_user_role] = optional_role
        return app, identity

    async def test_hidden_public_api_guards_all_roles_before_financial_side_effects(self):
        from app.api import marketplace
        async with self.sessions() as db:
            await db.execute(update(MarketplaceItem).where(MarketplaceItem.id == self.item_id).values(status="hidden"))
            await db.commit()
        app, identity = self.visibility_api()
        path = f"/marketplace/items/{self.item_id}"
        missing = f"/marketplace/items/{uuid4()}"
        roles = [("student", self.buyer_id), ("seller", self.seller_id), ("outsider", uuid4()),
                 ("professor", uuid4()), ("reviewer", uuid4()), ("admin", uuid4()), ("moderator", uuid4())]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid") as client:
            with patch.object(marketplace, "get_client", return_value=self.client), patch.object(marketplace, "get_object_stream") as storage:
                identity.update(id=None, role=None)
                self.assertEqual((await client.get(path)).status_code, 404)
                self.assertEqual((await client.post(path + "/purchase")).status_code, 401)
                self.assertEqual((await client.get(path + "/download")).status_code, 401)
                for role, user_id in roles:
                    with self.subTest(role=role):
                        identity.update(id=user_id, role="student" if role in {"seller", "outsider"} else role)
                        detail = await client.get(path)
                        self.assertEqual(detail.status_code, 200 if role in {"admin", "moderator"} else 404)
                        if detail.status_code == 200:
                            self.assertEqual(detail.json()["status"], "hidden")
                        hidden_purchase = await client.post(path + "/purchase")
                        missing_purchase = await client.post(missing + "/purchase")
                        self.assertEqual(hidden_purchase.status_code, 404)
                        self.assertEqual(hidden_purchase.json(), missing_purchase.json(), "hidden and missing purchase targets must be indistinguishable")
                        for suffix in ("/purchased", "/purchase-status", "/download"):
                            self.assertEqual((await client.get(path + suffix)).status_code, 404)
                        self.assertEqual((await client.get("/marketplace/items")).json()["items"], [])
                storage.assert_not_called()
        self.assertEqual(self.transport.requests, 0)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(MarketplacePurchaseOperation)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(MarketplaceSettlementOutbox)), 0)
        await self.assert_grants(0)

    async def test_download_and_history_preserve_buyer_owner_and_hidden_boundaries(self):
        from app.api import marketplace
        hidden_id, others_item_id = uuid4(), uuid4()
        async with self.sessions() as db:
            await db.execute(update(MarketplaceItem).where(MarketplaceItem.id == self.item_id).values(file_url="fixture/visible.txt", original_filename="visible.txt"))
            db.add_all([
                MarketplaceItem(id=hidden_id, title="hidden fixture", price=50, seller_id=self.seller_id, seller_name="seller", status="hidden", file_url="fixture/hidden.txt"),
                MarketplaceItem(id=others_item_id, title="other buyer fixture", price=50, seller_id=self.seller_id, seller_name="seller"),
            ])
            await db.flush()
            db.add_all([
                MarketplacePurchase(item_id=self.item_id, buyer_id=self.buyer_id, price_paid=100, seller_earned=20),
                MarketplacePurchase(item_id=hidden_id, buyer_id=self.buyer_id, price_paid=50, seller_earned=10),
                MarketplacePurchase(item_id=others_item_id, buyer_id=uuid4(), price_paid=50, seller_earned=10),
            ])
            await db.commit()
        app, identity = self.visibility_api()
        path = f"/marketplace/items/{self.item_id}"
        roles = [("student", self.buyer_id), ("seller", self.seller_id), ("outsider", uuid4()),
                 ("admin", uuid4()), ("moderator", uuid4())]
        payload = b"authorized fixture bytes"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid") as client:
            with patch.object(marketplace, "get_object_stream", side_effect=lambda _: (iter([payload]), "text/plain", len(payload))) as storage:
                identity.update(id=None, role=None)
                self.assertEqual((await client.get(path + "/download")).status_code, 401)
                for role, user_id in roles:
                    with self.subTest(role=role):
                        identity.update(id=user_id, role="student" if role in {"seller", "outsider"} else role)
                        downloaded = await client.get(path + "/download")
                        self.assertEqual(downloaded.status_code, 200 if role in {"student", "seller"} else 403)
                        if downloaded.status_code == 200:
                            self.assertEqual(downloaded.content, payload)
                        self.assertEqual((await client.get(f"/marketplace/items/{hidden_id}/download")).status_code, 404)
                        history = (await client.get("/marketplace/my/purchases")).json()
                        self.assertEqual([entry["item_id"] for entry in history], [str(self.item_id)] if role == "student" else [])
                        own_items = (await client.get("/marketplace/my/items")).json()
                        self.assertEqual({entry["id"] for entry in own_items}, {str(self.item_id), str(others_item_id)} if role == "seller" else set())
                        public_items = (await client.get("/marketplace/items")).json()["items"]
                        self.assertNotIn(str(hidden_id), [entry["id"] for entry in public_items])
                self.assertEqual(storage.call_count, 2, "only the actual buyer and seller may reach file storage")
        self.assertEqual(self.transport.requests, 0)

    async def test_hidden_after_preparation_preserves_private_settlement_but_blocks_content(self):
        from app.workers.marketplace_settlement_runner import run_once
        operation_id = (await self.prepare())["operation_id"]
        async with self.sessions() as db:
            await db.execute(update(MarketplaceItem).where(MarketplaceItem.id == self.item_id).values(status="hidden", file_url="fixture/hidden.txt"))
            await db.commit()
        app, identity = self.visibility_api()
        path = f"/marketplace/items/{self.item_id}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid") as client:
            pending = await client.get(path + "/purchase-status")
            self.assertEqual(pending.status_code, 202)
            self.assertEqual(pending.json()["operation_id"], operation_id)
            self.assertEqual(self.transport.requests, 0, "private status reads must never submit a transfer")
            self.assertEqual((await client.post(path + "/purchase")).status_code, 404)
            self.assertEqual(await run_once(self.sessions, self.gateway, batch_size=10, lease_seconds=60), 1)
            completed = await client.get(path + "/purchase-status")
            self.assertEqual(completed.status_code, 200)
            self.assertTrue(completed.json()["purchased"])
            self.assertEqual((await client.get(path + "/download")).status_code, 404)
            self.assertEqual((await client.get(path + "/purchased")).status_code, 404)
            self.assertEqual((await client.get("/marketplace/my/purchases")).json(), [])
            identity.update(id=uuid4(), role="admin")
            self.assertEqual((await client.get(path + "/purchase-status")).status_code, 404)
        self.assertEqual(self.transport.posts, 1)
        await self.assert_grants(1)

    async def test_concurrent_preparation_persists_single_operation_and_outbox(self):
        outcomes = await asyncio.gather(*(self.prepare() for _ in range(8)))
        self.assertEqual(len({result["operation_id"] for result in outcomes}), 1)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(MarketplacePurchaseOperation)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(MarketplaceSettlementOutbox)), 1)
        self.assertEqual(self.transport.posts, 0)

    async def test_lost_commit_reply_stays_processing_then_grants_without_second_post(self):
        operation_id = (await self.prepare())["operation_id"]
        self.transport.lose_reply = True
        self.assertEqual((await self.reconcile(operation_id))["status"], "processing")
        await self.assert_grants(0)
        self.assertEqual((await self.reconcile(operation_id))["status"], "completed")
        self.assertEqual(self.transport.posts, 1)
        await self.assert_grants(1)

    async def test_worker_recovers_committed_transfer_without_browser_or_second_post(self):
        from app.workers.marketplace_settlement_runner import run_once
        operation_id = (await self.prepare())["operation_id"]
        self.transport.lose_reply = True
        await self.reconcile(operation_id)
        async with self.sessions() as db:
            await db.execute(update(MarketplaceSettlementOutbox).values(available_at=utc_now()))
            await db.commit()
        self.assertEqual(await run_once(self.sessions, self.gateway, batch_size=10, lease_seconds=60), 1)
        self.assertEqual(self.transport.posts, 1)
        await self.assert_grants(1)

    async def test_concurrent_finalize_grants_and_counts_once(self):
        operation_id = (await self.prepare())["operation_id"]
        results = await asyncio.gather(*(self.reconcile(operation_id) for _ in range(8)))
        self.assertEqual([result["status"] for result in results], ["completed"] * 8)
        self.assertEqual(len(self.transport.ledger), 1)
        await self.assert_grants(1)

    async def test_price_changes_do_not_change_pending_snapshot(self):
        first = await self.prepare()
        async with self.sessions() as db:
            await db.execute(update(MarketplaceItem).values(price=300))
            await db.commit()
        second = await self.prepare()
        self.assertEqual(second["operation_id"], first["operation_id"])
        self.assertEqual(second["price_paid"], 100)
        result = await self.reconcile(first["operation_id"])
        self.assertEqual(result["price_paid"], 100)

    async def test_changed_price_requires_confirmation_before_new_operation(self):
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as caught:
                await prepare_purchase(db, self.item_id, self.buyer_id, expected_price=75)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(await db.scalar(select(func.count()).select_from(MarketplacePurchaseOperation)), 0)
        self.assertEqual(self.transport.requests, 0)

    async def test_rejection_is_no_grant_and_explicit_retry_uses_new_operation(self):
        first = await self.prepare()
        self.transport.reject = True
        result = await self.reconcile(first["operation_id"])
        self.assertEqual(result["failure_code"], "insufficient_points")
        await self.assert_grants(0)
        second = await self.prepare()
        self.assertNotEqual(first["operation_id"], second["operation_id"])
        self.transport.reject = False
        self.assertEqual((await self.reconcile(second["operation_id"]))["status"], "completed")
        await self.assert_grants(1)

    async def test_lease_recovery_and_stale_claim_do_not_send_a_transfer(self):
        from datetime import timedelta
        operation_id = UUID((await self.prepare())["operation_id"])
        async with self.sessions() as db:
            first = (await claim_settlements(db))[0]
        async with self.sessions() as db:
            self.assertEqual(await claim_settlements(db), [])
            await db.execute(update(MarketplaceSettlementOutbox).values(lease_expires_at=utc_now() - timedelta(seconds=1)))
            await db.commit()
            second = (await claim_settlements(db))[0]
        self.assertNotEqual(first.lease_token, second.lease_token)
        await self.reconcile(operation_id, lease_token=first.lease_token)
        self.assertEqual(self.transport.requests, 0)
        self.assertEqual((await self.reconcile(operation_id, lease_token=second.lease_token))["status"], "completed")

    async def test_processing_item_cannot_be_hard_deleted(self):
        await self.prepare()
        async with self.sessions() as db:
            await db.execute(select(MarketplaceItem).where(MarketplaceItem.id == self.item_id).with_for_update())
            with self.assertRaises(HTTPException) as caught:
                await assert_no_pending_purchase(db, self.item_id)
            self.assertEqual(caught.exception.status_code, 409)

    async def test_response_mismatch_is_blocked_and_no_automatic_new_charge(self):
        operation_id = (await self.prepare())["operation_id"]
        self.transport.mismatch = True
        result = await self.reconcile(operation_id)
        self.assertEqual(result["status"], "processing")
        self.assertFalse(result["retryable"])
        request_count = self.transport.requests
        await self.reconcile(operation_id)
        self.assertEqual(request_count, self.transport.requests)
        await self.assert_grants(0)

    async def test_legacy_purchase_does_not_start_a_transfer_and_status_is_buyer_scoped(self):
        async with self.sessions() as db:
            db.add(MarketplacePurchase(item_id=self.item_id, buyer_id=self.buyer_id, price_paid=75, seller_earned=15))
            await db.commit()
        result = await self.prepare()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["price_paid"], 75)
        self.assertEqual(self.transport.requests, 0)
        async with self.sessions() as db:
            self.assertEqual((await purchase_status(db, self.item_id, uuid4()))["status"], "not_started")

    async def test_digest_snapshot_matches_fixed_transfer_contract(self):
        result = await self.prepare()
        async with self.sessions() as db:
            operation = await db.get(MarketplacePurchaseOperation, UUID(result["operation_id"]))
            self.assertEqual(operation.payload_digest, transfer_digest(self.buyer_id, self.seller_id, 100, 20))

    async def test_public_api_processing_completion_and_owner_scoped_status(self):
        from app.api import marketplace
        from app.api.deps import get_current_user_id
        from app.database import get_db
        app = FastAPI()
        app.include_router(marketplace.router)
        async def test_db():
            async with self.sessions() as db:
                yield db
        async def buyer():
            return self.buyer_id
        async def outsider():
            return uuid4()
        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[get_current_user_id] = buyer
        self.transport.lose_reply = True
        path = f"/marketplace/items/{self.item_id}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid") as client:
            with patch.object(marketplace, "get_client", return_value=self.client):
                pending = await client.post(path + "/purchase", json={"expected_price": 100})
                self.assertEqual(pending.status_code, 202)
                self.assertFalse(pending.json()["purchased"])
                status = await client.get(path + "/purchase-status")
                self.assertEqual(status.status_code, 202)
                self.assertEqual(status.json()["operation_id"], pending.json()["operation_id"])
                completed = await client.post(path + "/purchase")
                self.assertEqual(completed.status_code, 200)
                self.assertTrue(completed.json()["purchased"])
                self.assertEqual(completed.json()["price_paid"], 100)
                app.dependency_overrides[get_current_user_id] = outsider
                own_status = await client.get(path + "/purchase-status")
                self.assertEqual(own_status.json()["status"], "not_started")
        self.assertEqual(self.transport.posts, 1)
        await self.assert_grants(1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
