"""제출 감사 조회의 명시적 시각과 원문 열람 사유 계약."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from app.schemas.education import validate_safe_text


def _parse_audit_datetime(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("시간대가 포함된 ISO 8601 시각이 필요합니다.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("시간대가 포함된 ISO 8601 시각이 필요합니다.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("시간대가 포함된 ISO 8601 시각이 필요합니다.")
    return value


SubmissionAuditDateTime = Annotated[datetime, BeforeValidator(_parse_audit_datetime)]


class SubmissionFlagRevealRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return validate_safe_text(value, field_name="열람 사유")
