"""강의·과제 교육 운영 API 계약.

본문 필드는 TipTap HTML을 정규화해 저장하며, 기존 Markdown 본문은 읽기 호환한다.
제목·사유 같은 일반 텍스트와 본문 HTML은 서로 다른 검증 경계를 사용한다.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.utils.safe_html import sanitize_rich_content


EducationRoleValue = Literal["professor_admin"]
TeachingClassWritableStatus = Literal["draft", "published"]
TeachingClassStatusValue = Literal["draft", "published", "archived"]
EnrollmentStatusValue = Literal["active", "dropped"]
AssignmentWritableStatus = Literal["draft", "published"]
AssignmentStatusValue = Literal["draft", "published", "archived"]
EducationResourceTypeValue = Literal["course", "lesson", "wargame"]
GradeStatusValue = Literal["draft", "published"]

_HTML_TAG_PATTERN = re.compile(r"</?(?!https?://)[A-Za-z][^>]*>", re.IGNORECASE)
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalize_utc_datetime(value: datetime, *, field_name: str) -> datetime:
    """시간대 없는 시각을 거부하고 UTC로 정규화한다."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name}에는 시간대가 포함된 ISO 8601 시각이 필요합니다.")
    return value.astimezone(timezone.utc)


def validate_safe_text(value: str, *, field_name: str, allow_empty: bool = False) -> str:
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{field_name}은(는) 비워 둘 수 없습니다.")
    if _CONTROL_CHARACTER_PATTERN.search(normalized):
        raise ValueError(f"{field_name}에 제어 문자를 포함할 수 없습니다.")
    if _HTML_TAG_PATTERN.search(normalized):
        raise ValueError(f"{field_name}에 raw HTML 태그를 포함할 수 없습니다.")
    return normalized


def validate_safe_markdown(
    value: str,
    *,
    field_name: str,
    max_visible_chars: int,
) -> str:
    return sanitize_rich_content(
        value,
        field_name=field_name,
        max_visible_chars=max_visible_chars,
    )


def validate_time_window(
    *,
    starts_at: datetime | None,
    ends_at: datetime | None,
    starts_field: str,
    ends_field: str,
) -> None:
    if starts_at is not None and ends_at is not None and ends_at <= starts_at:
        raise ValueError(f"{ends_field}은(는) {starts_field}보다 늦어야 합니다.")


class EducationRoleGrantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: UUID
    role: EducationRoleValue = "professor_admin"
    reason: str = Field(..., min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return validate_safe_text(value, field_name="권한 부여 사유")


class EducationRoleGrantRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return validate_safe_text(value, field_name="권한 회수 사유")


class TeachingClassCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=200)
    summary: str | None = Field(None, max_length=500)
    description_markdown: str = Field(..., min_length=1)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    status: TeachingClassWritableStatus = "draft"

    @field_validator("title", "summary")
    @classmethod
    def validate_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return validate_safe_text(value, field_name=info.field_name)

    @field_validator("description_markdown")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return validate_safe_markdown(
            value,
            field_name="강의 설명",
            max_visible_chars=20000,
        )

    @field_validator("starts_at", "ends_at")
    @classmethod
    def normalize_datetimes(cls, value: datetime | None, info) -> datetime | None:
        if value is None:
            return None
        return normalize_utc_datetime(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_schedule(self):
        validate_time_window(
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            starts_field="starts_at",
            ends_field="ends_at",
        )
        return self


class TeachingClassUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, min_length=1, max_length=200)
    summary: str | None = Field(None, max_length=500)
    description_markdown: str | None = Field(None, min_length=1)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    status: TeachingClassWritableStatus | None = None

    @field_validator("title", "summary")
    @classmethod
    def validate_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return validate_safe_text(value, field_name=info.field_name)

    @field_validator("description_markdown")
    @classmethod
    def validate_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_safe_markdown(
            value,
            field_name="강의 설명",
            max_visible_chars=20000,
        )

    @field_validator("starts_at", "ends_at")
    @classmethod
    def normalize_datetimes(cls, value: datetime | None, info) -> datetime | None:
        if value is None:
            return None
        return normalize_utc_datetime(value, field_name=info.field_name)


class ArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return validate_safe_text(value, field_name="보관 사유")


class TeachingClassRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TeachingClassWritableStatus = "draft"


class EnrollmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_id: UUID


class EnrollmentDropRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return validate_safe_text(value, field_name="수강 취소 사유")


class EducationResourceLinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_type: EducationResourceTypeValue
    course_id: UUID | None = None
    lesson_id: UUID | None = None
    challenge_id: int | None = Field(None, ge=1)
    label: str | None = Field(None, max_length=200)

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_safe_text(value, field_name="표시 이름")

    @model_validator(mode="after")
    def validate_exactly_one_public_content_target(self):
        targets = {
            "course": self.course_id,
            "lesson": self.lesson_id,
            "wargame": self.challenge_id,
        }
        selected = [name for name, value in targets.items() if value is not None]
        if selected != [self.resource_type]:
            raise ValueError("resource_type에 맞는 대상 하나만 지정해야 합니다.")
        return self


class EducationAssignmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=200)
    instructions_markdown: str = Field(..., min_length=1)
    max_score: int = Field(100, ge=1, le=1000)
    available_from_at: datetime | None = None
    due_at: datetime | None = None
    status: AssignmentWritableStatus = "draft"

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return validate_safe_text(value, field_name="과제 제목")

    @field_validator("instructions_markdown")
    @classmethod
    def validate_instructions(cls, value: str) -> str:
        return validate_safe_markdown(
            value,
            field_name="과제 안내",
            max_visible_chars=20000,
        )

    @field_validator("available_from_at", "due_at")
    @classmethod
    def normalize_datetimes(cls, value: datetime | None, info) -> datetime | None:
        if value is None:
            return None
        return normalize_utc_datetime(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_submission_window(self):
        validate_time_window(
            starts_at=self.available_from_at,
            ends_at=self.due_at,
            starts_field="available_from_at",
            ends_field="due_at",
        )
        return self


class EducationAssignmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, min_length=1, max_length=200)
    instructions_markdown: str | None = Field(None, min_length=1)
    max_score: int | None = Field(None, ge=1, le=1000)
    available_from_at: datetime | None = None
    due_at: datetime | None = None
    status: AssignmentWritableStatus | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_safe_text(value, field_name="과제 제목")

    @field_validator("instructions_markdown")
    @classmethod
    def validate_instructions(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_safe_markdown(
            value,
            field_name="과제 안내",
            max_visible_chars=20000,
        )

    @field_validator("available_from_at", "due_at")
    @classmethod
    def normalize_datetimes(cls, value: datetime | None, info) -> datetime | None:
        if value is None:
            return None
        return normalize_utc_datetime(value, field_name=info.field_name)


class AssignmentRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AssignmentWritableStatus = "draft"


class AssignmentSubmissionUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_markdown: str = Field(..., min_length=1)

    @field_validator("content_markdown")
    @classmethod
    def validate_submission(cls, value: str) -> str:
        return validate_safe_markdown(
            value,
            field_name="제출 본문",
            max_visible_chars=30000,
        )


class AssignmentGradeUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(..., ge=0, le=1000)
    feedback_markdown: str | None = None
    status: GradeStatusValue = "draft"

    @field_validator("feedback_markdown")
    @classmethod
    def validate_feedback(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_safe_markdown(
            value,
            field_name="채점 피드백",
            max_visible_chars=12000,
        )


class EducationAssetDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return validate_safe_text(value, field_name="자료 삭제 사유")


class EducationRoleGrantResponse(BaseModel):
    id: UUID
    subject_id: UUID
    role: EducationRoleValue
    granted_by: UUID
    grant_reason: str
    granted_at: datetime
    revoked_by: UUID | None = None
    revoke_reason: str | None = None
    revoked_at: datetime | None = None
    active: bool


class EducationPermissionsResponse(BaseModel):
    """프런트엔드가 교육 대시보드 진입을 판정하는 현재 사용자 own-state."""

    user_id: UUID
    is_education_manager: bool
    is_platform_admin: bool
    education_role: Literal["professor_admin"] | None = None


class TeachingClassSummary(BaseModel):
    id: UUID
    title: str
    summary: str | None = None
    instructor_id: UUID
    status: TeachingClassStatusValue
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    created_at: datetime


class EducationResourceLinkResponse(BaseModel):
    id: UUID
    resource_type: EducationResourceTypeValue
    course_id: UUID | None = None
    lesson_id: UUID | None = None
    challenge_id: int | None = None
    label: str | None = None
    # The API, rather than the browser, resolves the target into a safe
    # same-origin route.  A stale or no-longer-public target deliberately has
    # no href, even for an education manager reviewing the stale link.
    href: str | None = None
    created_at: datetime


class TeachingClassDetail(TeachingClassSummary):
    description_markdown: str
    published_at: datetime | None = None
    archived_at: datetime | None = None
    archive_reason: str | None = None
    resources: list[EducationResourceLinkResponse] = Field(default_factory=list)
    is_enrolled: bool = False
    is_education_manager: bool = False


class TeachingClassListResponse(BaseModel):
    items: list[TeachingClassSummary]
    total: int
    page: int
    size: int


class EnrollmentResponse(BaseModel):
    id: UUID
    class_id: UUID
    student_id: UUID
    status: EnrollmentStatusValue
    enrolled_at: datetime
    dropped_at: datetime | None = None


class EducationAssignmentSummary(BaseModel):
    id: UUID
    class_id: UUID
    title: str
    status: AssignmentStatusValue
    max_score: int
    available_from_at: datetime | None = None
    due_at: datetime | None = None
    created_at: datetime


class EducationAssignmentDetail(EducationAssignmentSummary):
    instructions_markdown: str
    published_at: datetime | None = None
    archived_at: datetime | None = None
    archive_reason: str | None = None


class EducationAssignmentListResponse(BaseModel):
    items: list[EducationAssignmentSummary]
    total: int
    page: int
    size: int


class AssignmentSubmissionMetadata(BaseModel):
    id: UUID
    assignment_id: UUID
    student_id: UUID
    submitted_at: datetime
    updated_at: datetime
    content_length: int
    grade_status: GradeStatusValue | None = None


class AssignmentSubmissionDetail(AssignmentSubmissionMetadata):
    content_markdown: str


class AssignmentSubmissionListResponse(BaseModel):
    items: list[AssignmentSubmissionMetadata]
    total: int
    page: int
    size: int


class AssignmentGradeResponse(BaseModel):
    id: UUID
    submission_id: UUID
    score: int
    status: GradeStatusValue
    graded_by: UUID
    graded_at: datetime
    published_at: datetime | None = None
    feedback_markdown: str | None = None


class EducationAssetResponse(BaseModel):
    """private object_path를 제외한 자료 메타데이터."""

    id: UUID
    class_id: UUID
    assignment_id: UUID | None = None
    scope: Literal["class", "assignment"]
    stored_name: str
    original_name: str
    content_type: str
    byte_size: int
    uploaded_by: UUID
    created_at: datetime
    deleted_at: datetime | None = None


class EducationAssetListResponse(BaseModel):
    items: list[EducationAssetResponse]
    total: int
    page: int
    size: int


class EducationAssetUsageResponse(BaseModel):
    """교수 대시보드의 수업 단위 private 자료 quota 표시용."""

    class_id: UUID
    active_asset_count: int
    active_byte_size: int
    max_assets: int
    max_byte_size: int
    max_file_size: int


class EducationAuditEventResponse(BaseModel):
    id: UUID
    actor_id: UUID
    action: str
    entity_type: str
    entity_id: UUID
    class_id: UUID | None = None
    details: dict
    created_at: datetime


class EducationAuditListResponse(BaseModel):
    items: list[EducationAuditEventResponse]
    total: int
    page: int
    size: int
