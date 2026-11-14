"""장터 정산: 영속 작업 생성, Auth 확인, 권한 부여, lease 기반 복구."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.marketplace import MarketplaceItem, MarketplacePurchase
from app.models.marketplace_settlement import MarketplacePurchaseOperation, MarketplaceSettlementOutbox


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def transfer_digest(buyer_id: UUID, seller_id: UUID, price_paid: int, seller_earned: int) -> str:
    payload = {
        "contract": "marketplace_transfer_v1",
        "buyer_id": str(buyer_id),
        "seller_id": str(seller_id),
        "price_paid": price_paid,
        "seller_earned": seller_earned,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()


def operation_response(operation: MarketplacePurchaseOperation, *, blocked: bool = False) -> dict:
    messages = {
        "completed": "구매가 완료되었습니다.",
        "processing": "결제를 확인하고 있습니다. 처리 상태를 다시 확인해 주세요.",
        "rejected": "구매를 완료하지 못했습니다.",
    }
    if operation.failure_code == "insufficient_points":
        messages["rejected"] = "포인트가 부족합니다."
    elif operation.failure_code == "account_unavailable":
        messages["rejected"] = "구매자 또는 판매자 계정을 찾을 수 없습니다."
    if blocked:
        messages["processing"] = "결제 확인에 관리자 검토가 필요합니다. 관리자에게 문의해 주세요."
    return {
        "operation_id": str(operation.id),
        "status": operation.state,
        "purchased": operation.state == "completed",
        "price_paid": operation.price_paid,
        "seller_earned": operation.seller_earned,
        "failure_code": operation.failure_code,
        "retryable": operation.state == "processing" and not blocked,
        "message": messages[operation.state],
    }


def purchase_response(purchase: MarketplacePurchase) -> dict:
    return {
        "operation_id": None,
        "status": "completed",
        "purchased": True,
        "price_paid": purchase.price_paid,
        "seller_earned": purchase.seller_earned,
        "failure_code": None,
        "retryable": False,
        "message": "구매가 완료되었습니다.",
    }


async def purchase_status(db: AsyncSession, item_id: UUID, buyer_id: UUID) -> dict:
    purchase = (await db.execute(select(MarketplacePurchase).where(
        MarketplacePurchase.item_id == item_id, MarketplacePurchase.buyer_id == buyer_id,
    ))).scalar_one_or_none()
    if purchase is not None:
        return purchase_response(purchase)
    operation = (await db.execute(select(MarketplacePurchaseOperation).where(
        MarketplacePurchaseOperation.item_id == item_id, MarketplacePurchaseOperation.buyer_id == buyer_id,
    ).order_by(MarketplacePurchaseOperation.created_at.desc(), MarketplacePurchaseOperation.id.desc()).limit(1))).scalar_one_or_none()
    if operation is None:
        return {"status": "not_started", "purchased": False, "operation_id": None, "retryable": False}
    outbox = (await db.execute(select(MarketplaceSettlementOutbox).where(
        MarketplaceSettlementOutbox.operation_id == operation.id,
    ))).scalar_one_or_none()
    return operation_response(operation, blocked=outbox is not None and outbox.state == "blocked")


async def prepare_purchase(db: AsyncSession, item_id: UUID, buyer_id: UUID, *, expected_price: int | None = None) -> dict:
    """item lock이 snapshot·중복 요청·삭제를 직렬화한다. 원격 요청 전에 commit한다."""
    item = (await db.execute(select(MarketplaceItem).where(
        MarketplaceItem.id == item_id,
    ).with_for_update())).scalar_one_or_none()
    if item is None or item.status != "active":
        raise HTTPException(status_code=404, detail="구매할 수 있는 아이템을 찾을 수 없습니다.")
    if item.seller_id == buyer_id:
        raise HTTPException(status_code=400, detail="자신의 아이템은 구매할 수 없습니다.")
    current = await purchase_status(db, item_id, buyer_id)
    if current["status"] in {"processing", "completed"}:
        await db.commit()
        return current
    if expected_price is not None and expected_price != item.price:
        raise HTTPException(status_code=409, detail="상품 가격이 변경되었습니다. 새로고침 후 가격을 확인해 주세요.")
    operation_id = uuid4()
    earned = max(1, item.price * 20 // 100)
    operation = MarketplacePurchaseOperation(
        id=operation_id, item_id=item.id, buyer_id=buyer_id, seller_id=item.seller_id,
        price_paid=item.price, seller_earned=earned,
        payload_digest=transfer_digest(buyer_id, item.seller_id, item.price, earned),
        state="processing",
    )
    db.add(operation)
    await db.flush()
    db.add(MarketplaceSettlementOutbox(operation_id=operation_id))
    await db.flush()
    result = operation_response(operation)
    await db.commit()
    return result


@dataclass(frozen=True)
class TransferOutcome:
    state: str
    failure_code: str | None = None


class TransferGateway:
    def __init__(self, client: httpx.AsyncClient, auth_service_url: str):
        self.client = client
        self.base_url = auth_service_url.rstrip("/")

    async def confirm(self, operation: MarketplacePurchaseOperation) -> TransferOutcome:
        payload = {
            "operation_id": str(operation.id), "buyer_id": str(operation.buyer_id),
            "seller_id": str(operation.seller_id), "price_paid": operation.price_paid,
            "seller_earned": operation.seller_earned, "payload_digest": operation.payload_digest,
        }
        try:
            response = await self.client.get(f"{self.base_url}/internal/points/transfers/{operation.id}")
            if response.status_code == 404:
                response = await self.client.post(f"{self.base_url}/internal/points/transfers", json=payload)
            if response.status_code == 409:
                return TransferOutcome("blocked", "payload_conflict")
            if response.status_code != 200:
                return TransferOutcome("unknown", "auth_unavailable")
            data = response.json()
            if (
                not isinstance(data, dict)
                or data.get("operation_id") != str(operation.id)
                or data.get("payload_digest") != operation.payload_digest
                or type(data.get("price_paid")) is not int or data["price_paid"] != operation.price_paid
                or type(data.get("seller_earned")) is not int or data["seller_earned"] != operation.seller_earned
                or data.get("state") not in {"completed", "rejected"}
                or data.get("failure_code") not in {None, "insufficient_points", "account_unavailable"}
                or (data.get("state") == "completed" and data.get("failure_code") is not None)
                or (data.get("state") == "rejected" and data.get("failure_code") is None)
            ):
                return TransferOutcome("blocked", "response_mismatch")
            return TransferOutcome(data["state"], data.get("failure_code"))
        except (httpx.RequestError, ValueError):
            return TransferOutcome("unknown", "auth_unavailable")


async def reconcile_operation(
    db: AsyncSession, operation_id: UUID, gateway: TransferGateway, *, lease_token: UUID | None = None,
) -> dict:
    operation = (await db.execute(select(MarketplacePurchaseOperation).where(
        MarketplacePurchaseOperation.id == operation_id,
    ))).scalar_one()
    item_id, buyer_id = operation.item_id, operation.buyer_id
    outbox = (await db.execute(select(MarketplaceSettlementOutbox).where(
        MarketplaceSettlementOutbox.operation_id == operation_id,
    ))).scalar_one()
    if (
        operation.state != "processing" or outbox.state == "blocked"
        or (lease_token is not None and (outbox.state != "leased" or outbox.lease_token != lease_token))
    ):
        result = operation_response(operation, blocked=outbox.state == "blocked")
        await db.commit()
        return result
    # expire_on_commit=False 세션은 snapshot을 유지하고 HTTP 중 DB lock은 해제한다.
    await db.commit()
    outcome = await gateway.confirm(operation)
    item = (await db.execute(select(MarketplaceItem).where(
        MarketplaceItem.id == item_id,
    ).with_for_update())).scalar_one_or_none()
    operation = (await db.execute(select(MarketplacePurchaseOperation).where(
        MarketplacePurchaseOperation.id == operation_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one()
    outbox = (await db.execute(select(MarketplaceSettlementOutbox).where(
        MarketplaceSettlementOutbox.operation_id == operation_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one()
    if operation.state != "processing" or outbox.state == "blocked":
        result = operation_response(operation, blocked=outbox.state == "blocked")
        await db.commit()
        return result

    if outcome.state == "completed" and item is None:
        outcome = TransferOutcome("blocked", "item_unavailable")
    if outcome.state == "completed":
        purchase = (await db.execute(select(MarketplacePurchase).where(
            MarketplacePurchase.item_id == item_id, MarketplacePurchase.buyer_id == buyer_id,
        ))).scalar_one_or_none()
        if purchase is None:
            db.add(MarketplacePurchase(
                item_id=item_id, buyer_id=buyer_id, price_paid=operation.price_paid,
                seller_earned=operation.seller_earned,
            ))
            await db.execute(update(MarketplaceItem).where(MarketplaceItem.id == item_id).values(
                download_count=MarketplaceItem.download_count + 1,
            ))
        operation.state = "completed"
    elif outcome.state == "rejected":
        operation.state = "rejected"
        operation.failure_code = outcome.failure_code
    elif outcome.state == "blocked":
        outbox.state = "blocked"
        outbox.last_error = outcome.failure_code
    elif outbox.state != "leased" or (lease_token is not None and outbox.lease_token == lease_token):
        outbox.state = "pending"
        outbox.available_at = utc_now() + timedelta(seconds=min(300, 5 * (2 ** min(outbox.attempt_count, 6))))
        outbox.last_error = outcome.failure_code

    if operation.state != "processing":
        operation.settled_at = utc_now()
        outbox.state = "done"
        outbox.last_error = None
    if outbox.state != "leased":
        outbox.lease_token = None
        outbox.lease_expires_at = None
    result = operation_response(operation, blocked=outbox.state == "blocked")
    await db.commit()
    return result


@dataclass(frozen=True)
class SettlementClaim:
    operation_id: UUID
    lease_token: UUID


async def claim_settlements(db: AsyncSession, *, batch_size: int = 10, lease_seconds: int = 60) -> list[SettlementClaim]:
    now = utc_now()
    rows = (await db.execute(select(MarketplaceSettlementOutbox).where(or_(
        and_(MarketplaceSettlementOutbox.state == "pending", MarketplaceSettlementOutbox.available_at <= now),
        and_(MarketplaceSettlementOutbox.state == "leased", MarketplaceSettlementOutbox.lease_expires_at <= now),
    )).order_by(MarketplaceSettlementOutbox.available_at).limit(batch_size).with_for_update(skip_locked=True))).scalars().all()
    for row in rows:
        row.state = "leased"
        row.lease_token = uuid4()
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        row.attempt_count += 1
    result = [SettlementClaim(row.operation_id, row.lease_token) for row in rows]
    await db.commit()
    return result


async def assert_no_pending_purchase(db: AsyncSession, item_id: UUID) -> None:
    pending = (await db.execute(select(MarketplacePurchaseOperation.id).where(
        MarketplacePurchaseOperation.item_id == item_id, MarketplacePurchaseOperation.state == "processing",
    ).limit(1))).scalar_one_or_none()
    if pending is not None:
        raise HTTPException(status_code=409, detail="결제 확인 중인 구매가 있어 아이템을 삭제할 수 없습니다.")
