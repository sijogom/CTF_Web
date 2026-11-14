"""밴 API 입력 계약."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


def _strip_nonempty(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("빈 값은 허용되지 않습니다.")
    return normalized


class ManualBanCreate(BaseModel):
    target_user_id: UUID
    public_reason: str = Field(min_length=3, max_length=1000)
    evidence_summary: str = Field(min_length=10, max_length=4000)
    policy_version: str = Field(min_length=1, max_length=100)
    duration_hours: int | None = Field(default=None, ge=1, le=24 * 365)
    permanent: bool = False
    appeal_allowed: bool = True

    @field_validator("public_reason", "evidence_summary", "policy_version")
    @classmethod
    def required_text(cls, value: str) -> str:
        return _strip_nonempty(value)

    @model_validator(mode="after")
    def validate_duration(self):
        if self.permanent == (self.duration_hours is not None):
            raise ValueError("영구 밴 또는 유효한 기간 중 하나를 명시해야 합니다.")
        return self


class BanLiftRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)

    @field_validator("reason")
    @classmethod
    def required_text(cls, value: str) -> str:
        return _strip_nonempty(value)


class BanAppealCreate(BaseModel):
    message: str = Field(min_length=10, max_length=4000)

    @field_validator("message")
    @classmethod
    def required_text(cls, value: str) -> str:
        return _strip_nonempty(value)


class BanAppealReview(BaseModel):
    decision: Literal["accepted", "rejected"]
    reviewer_note: str = Field(min_length=3, max_length=4000)
    lift_ban: bool = False
    lift_reason: str | None = Field(default=None, max_length=1000)

    @field_validator("reviewer_note")
    @classmethod
    def required_text(cls, value: str) -> str:
        return _strip_nonempty(value)

    @field_validator("lift_reason")
    @classmethod
    def optional_text(cls, value: str | None) -> str | None:
        return _strip_nonempty(value) if value is not None else None

    @model_validator(mode="after")
    def validate_lift_request(self):
        if self.lift_ban and self.decision != "accepted":
            raise ValueError("기각된 이의제기로는 밴을 해제할 수 없습니다.")
        if self.lift_ban and not self.lift_reason:
            raise ValueError("이의제기로 밴을 해제할 때는 해제 사유가 필요합니다.")
        return self
