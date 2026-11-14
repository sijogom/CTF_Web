"""플랫폼 이벤트 API의 입력·출력 계약."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.utils.event_visibility import normalize_utc_datetime, validate_event_schedule
from app.utils.safe_html import sanitize_rich_content


EventCategory = Annotated[str, Field(min_length=1, max_length=30, pattern=r"^[a-z][a-z0-9_-]*$")]
EventWritableStatus = Literal["draft", "published"]
EventStatusValue = Literal["draft", "published", "archived"]
EventPhase = Literal["upcoming", "active", "ended"]

_HTML_TAG_PATTERN = re.compile(r"</?(?!https?://)[A-Za-z][^>]*>", re.IGNORECASE)
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_CONTROL_OR_WHITESPACE_PATTERN = re.compile(r"[\x00-\x20\x7f]")


def _validate_safe_text(value: str, *, field_name: str, allow_empty: bool = False) -> str:
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{field_name}은(는) 비워 둘 수 없습니다.")
    if _CONTROL_CHARACTER_PATTERN.search(normalized):
        raise ValueError(f"{field_name}에 제어 문자를 포함할 수 없습니다.")
    if _HTML_TAG_PATTERN.search(normalized):
        raise ValueError(f"{field_name}에 raw HTML 태그를 포함할 수 없습니다.")
    return normalized


def _validate_safe_markdown(value: str) -> str:
    return sanitize_rich_content(
        value,
        field_name="본문",
        max_visible_chars=12000,
    )


def _validate_external_url(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.strip():
        return None
    if _URL_CONTROL_OR_WHITESPACE_PATTERN.search(value):
        raise ValueError("외부 링크에 공백 또는 제어 문자를 포함할 수 없습니다.")
    normalized = value.strip()
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("외부 링크는 사용자 정보가 없는 HTTP(S) URL이어야 합니다.")
    return normalized


class EventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=200)
    summary: str | None = Field(None, max_length=500)
    content_markdown: str = Field(..., min_length=1)
    category: EventCategory
    location: str | None = Field(None, max_length=300)
    external_url: str | None = Field(None, max_length=2048)
    starts_at: datetime
    ends_at: datetime
    visible_from_at: datetime
    visible_until_at: datetime | None = None
    status: EventWritableStatus = "draft"

    @field_validator("title", "summary", "location", mode="after")
    @classmethod
    def validate_safe_optional_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_safe_text(value, field_name=info.field_name)

    @field_validator("content_markdown", mode="after")
    @classmethod
    def validate_content_markdown(cls, value: str) -> str:
        return _validate_safe_markdown(value)

    @field_validator("external_url", mode="after")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        return _validate_external_url(value)

    @field_validator("starts_at", "ends_at", "visible_from_at", "visible_until_at", mode="after")
    @classmethod
    def normalize_event_datetimes(cls, value: datetime | None, info) -> datetime | None:
        if value is None:
            return None
        return normalize_utc_datetime(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_schedule(self):
        validate_event_schedule(
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            visible_from_at=self.visible_from_at,
            visible_until_at=self.visible_until_at,
        )
        return self


class EventUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, min_length=1, max_length=200)
    summary: str | None = Field(None, max_length=500)
    content_markdown: str | None = Field(None, min_length=1)
    category: EventCategory | None = None
    location: str | None = Field(None, max_length=300)
    external_url: str | None = Field(None, max_length=2048)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    visible_from_at: datetime | None = None
    visible_until_at: datetime | None = None
    status: EventWritableStatus | None = None

    @field_validator("title", "summary", "location", mode="after")
    @classmethod
    def validate_safe_optional_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_safe_text(value, field_name=info.field_name)

    @field_validator("content_markdown", mode="after")
    @classmethod
    def validate_content_markdown(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_safe_markdown(value)

    @field_validator("external_url", mode="after")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        return _validate_external_url(value)

    @field_validator("starts_at", "ends_at", "visible_from_at", "visible_until_at", mode="after")
    @classmethod
    def normalize_event_datetimes(cls, value: datetime | None, info) -> datetime | None:
        if value is None:
            return None
        return normalize_utc_datetime(value, field_name=info.field_name)


class EventArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=1000)

    @field_validator("reason", mode="after")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return _validate_safe_text(value, field_name="보관 사유")


class EventRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: EventWritableStatus = "draft"


class EventSummary(BaseModel):
    id: UUID
    title: str
    summary: str | None = None
    category: EventCategory
    category_labels: dict[str, str] = Field(default_factory=dict)
    location: str | None = None
    external_url: str | None = None
    starts_at: datetime
    ends_at: datetime
    event_phase: EventPhase


class EventPublicDetail(EventSummary):
    content_markdown: str
    visible_from_at: datetime
    visible_until_at: datetime | None = None


class EventAdminDetail(EventPublicDetail):
    status: EventStatusValue
    published_at: datetime | None = None
    archived_at: datetime | None = None
    archive_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class EventListResponse(BaseModel):
    items: list[EventSummary]
    total: int
    page: int
    size: int


class EventAdminListResponse(BaseModel):
    items: list[EventAdminDetail]
    total: int
    page: int
    size: int


class EventAuditEventResponse(BaseModel):
    id: UUID
    actor_id: UUID
    action: str
    details: dict
    created_at: datetime


class EventAuditListResponse(BaseModel):
    items: list[EventAuditEventResponse]
    total: int
    page: int
    size: int


class EventCategoryLabels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ko: str = Field(min_length=1, max_length=80)
    en: str = Field(min_length=1, max_length=80)
    ja: str = Field(min_length=1, max_length=80)
    zh: str = Field(min_length=1, max_length=80)

    @field_validator("ko", "en", "ja", "zh", mode="after")
    @classmethod
    def validate_label(cls, value: str) -> str:
        return _validate_safe_text(value, field_name="카테고리 이름")


class EventCategoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labels: EventCategoryLabels
    sort_order: int = Field(ge=0, le=1000000, strict=True)


class EventCategoryCreate(EventCategoryWrite):
    operation_id: UUID | None = None


class EventCategoryUpdate(EventCategoryWrite):
    expected_revision: int = Field(ge=1)


class EventCategoryResponse(EventCategoryWrite):
    key: EventCategory
    revision: int = Field(ge=1)


class EventCategoryListResponse(BaseModel):
    items: list[EventCategoryResponse]
