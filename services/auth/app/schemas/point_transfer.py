"""Community와 Auth 사이의 고정 장터 이체 계약."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


def marketplace_transfer_digest(buyer_id: UUID, seller_id: UUID, price_paid: int, seller_earned: int) -> str:
    payload = {
        "contract": "marketplace_transfer_v1",
        "buyer_id": str(buyer_id),
        "seller_id": str(seller_id),
        "price_paid": price_paid,
        "seller_earned": seller_earned,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()


class MarketplaceTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    buyer_id: UUID
    seller_id: UUID
    price_paid: int = Field(strict=True, ge=1, le=100000)
    seller_earned: int = Field(strict=True, ge=1, le=100000)
    payload_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_snapshot(self):
        if self.buyer_id == self.seller_id:
            raise ValueError("구매자와 판매자는 달라야 합니다.")
        if self.seller_earned != max(1, self.price_paid * 20 // 100):
            raise ValueError("판매자 지급액이 가격 snapshot 정책과 다릅니다.")
        expected = marketplace_transfer_digest(self.buyer_id, self.seller_id, self.price_paid, self.seller_earned)
        if self.payload_digest != expected:
            raise ValueError("이체 payload digest가 일치하지 않습니다.")
        return self


class MarketplaceTransferResponse(BaseModel):
    operation_id: UUID
    payload_digest: str
    state: Literal["completed", "rejected"]
    price_paid: int
    seller_earned: int
    failure_code: str | None
