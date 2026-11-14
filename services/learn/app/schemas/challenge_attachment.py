"""일반 문제 첨부의 공개 DTO. 저장소 경로는 참조 권한이 아니다."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator


class AttachmentFields(BaseModel):
    attachment_draft_id: UUID4 | None = None
    attachment_ids: list[UUID] | None = None

    @field_validator("attachment_draft_id", "attachment_ids", mode="before")
    @classmethod
    def explicit_null_is_not_omission(cls, value):
        if value is None:
            raise ValueError("첨부 필드는 null 대신 생략하거나 빈 목록을 사용하세요.")
        return value

    @field_validator("attachment_ids")
    @classmethod
    def unique_ids(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("첨부 ID는 중복될 수 없습니다.")
        return value


class AttachmentDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_id: UUID4
    challenge_id: Annotated[int, Field(strict=True, gt=0)] | None = None


class AttachmentUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    draft_id: UUID
    original_name: str
    byte_size: Annotated[int, Field(strict=True, ge=0, le=100 * 1024 * 1024)]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str
    state: Literal["pending", "ready", "missing"]
    ready_for_attach: bool
    reconcile_required: bool
    retry_allowed: bool


class ChallengeAttachmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID | None = None
    legacy_index: int | None = None
    name: str
    byte_size: int | None = None
    sha256: str | None = None
    state: Literal["ready", "unavailable"]
    download_path: str | None = None


class AttachmentDraftResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_id: UUID
    challenge_id: int | None
    state: Literal["open", "bound"]
    base_attachment_revision: int
    current_attachment_revision: int
    attachments: list[ChallengeAttachmentResponse]
    uploads: list[AttachmentUploadResponse]


class AttachmentReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
