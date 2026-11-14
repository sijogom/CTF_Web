"""포인트 차감·판매자 지급·멱등 결과를 같은 transaction으로 확정한다."""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.point_transfer import MarketplacePointTransfer
from app.models.user import User
from app.schemas.point_transfer import MarketplaceTransferRequest, MarketplaceTransferResponse


def transfer_response(transfer: MarketplacePointTransfer) -> MarketplaceTransferResponse:
    return MarketplaceTransferResponse(
        operation_id=transfer.operation_id,
        payload_digest=transfer.payload_digest,
        state=transfer.state,
        price_paid=transfer.price_paid,
        seller_earned=transfer.seller_earned,
        failure_code=transfer.failure_code,
    )


async def settle_marketplace_transfer(db: AsyncSession, body: MarketplaceTransferRequest) -> MarketplaceTransferResponse:
    try:
        inserted = await db.execute(
            insert(MarketplacePointTransfer).values(
                operation_id=body.operation_id,
                payload_digest=body.payload_digest,
                buyer_id=body.buyer_id,
                seller_id=body.seller_id,
                price_paid=body.price_paid,
                seller_earned=body.seller_earned,
                state="processing",
                created_at=datetime.now(timezone.utc),
            ).on_conflict_do_nothing(index_elements=["operation_id"]).returning(MarketplacePointTransfer.operation_id)
        )
        is_new = inserted.scalar_one_or_none() is not None
        transfer = (await db.execute(select(MarketplacePointTransfer).where(
            MarketplacePointTransfer.operation_id == body.operation_id,
        ))).scalar_one()
        if transfer.payload_digest != body.payload_digest:
            raise HTTPException(status_code=409, detail="동일한 작업 ID에 다른 이체 입력을 사용할 수 없습니다.")
        if not is_new:
            result = transfer_response(transfer)
            await db.commit()
            return result

        # UUID 순서의 row lock으로 반대 방향의 동시 구매도 같은 lock 순서를 따른다.
        users = (await db.execute(select(User).where(
            User.id.in_([body.buyer_id, body.seller_id]),
        ).order_by(User.id).with_for_update())).scalars().all()
        by_id = {user.id: user for user in users}
        buyer = by_id.get(body.buyer_id)
        seller = by_id.get(body.seller_id)
        if buyer is None or seller is None:
            transfer.state = "rejected"
            transfer.failure_code = "account_unavailable"
        elif buyer.points < body.price_paid:
            transfer.state = "rejected"
            transfer.failure_code = "insufficient_points"
        else:
            buyer.points -= body.price_paid
            seller.points += body.seller_earned
            transfer.state = "completed"
        transfer.settled_at = datetime.now(timezone.utc)
        result = transfer_response(transfer)
        await db.commit()
        return result
    except BaseException:
        await db.rollback()
        raise
