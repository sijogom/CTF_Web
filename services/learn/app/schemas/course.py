from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator
from app.schemas.knowledge import ContentTagMappingUpdate

from app.utils.safe_html import sanitize_rich_content


# ──── 요청 ────

class CourseCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    category: str = Field(..., pattern=r"^(web|pwn|rev|crypto|forensic|misc)$")
    difficulty: str = Field(..., pattern=r"^(introductory|basic|intermediate|advanced)$")
    estimated_hours: int = Field(1, ge=1, le=100)
    thumbnail_url: str | None = None
    knowledge_tag_ids: list[int] = Field(default_factory=list, max_length=50)

    @field_validator("knowledge_tag_ids")
    @classmethod
    def unique_knowledge_tag_ids(cls, value: list[int]) -> list[int]:
        if any(tag_id <= 0 for tag_id in value) or len(set(value)) != len(value):
            raise ValueError("지식 태그 ID 목록은 양의 정수이며 중복될 수 없습니다.")
        return value

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_rich_content(
            value,
            field_name="코스 설명",
            allow_empty=True,
        )


class CourseMetadataUpdate(BaseModel):
    """Optimistic, title/description-only course metadata update."""

    expected_metadata_revision: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    knowledge_tag_mapping: ContentTagMappingUpdate | None = None

    model_config = {"extra": "forbid"}

    @field_validator("title")
    @classmethod
    def reject_explicit_null_title(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("코스 제목은 null일 수 없습니다.")
        return value


class CourseSubmit(BaseModel):
    """코스 승인 제출 요청 (draft → pending)."""
    pass


class CourseReject(BaseModel):
    """코스 거부 요청 (관리자 전용)."""
    rejection_reason: str = Field(..., min_length=1, max_length=1000, description="거부 사유")


class CoursePrerequisiteReplace(BaseModel):
    """코스의 강제 선행 코스를 전체 교체한다."""

    prerequisite_ids: list[UUID] = Field(default_factory=list, max_length=50)

    @field_validator("prerequisite_ids")
    @classmethod
    def unique_prerequisite_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("같은 선행 코스를 중복해서 지정할 수 없습니다.")
        return value


class CoursePrerequisiteResponse(BaseModel):
    course_id: UUID
    prerequisite_ids: list[UUID]


class LessonCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str | None = None
    order_index: int = Field(0, ge=0)
    estimated_minutes: int = Field(10, ge=1, le=600)
    challenge_id: int | None = None
    knowledge_tag_ids: list[int] = Field(default_factory=list, max_length=50)

    @field_validator("knowledge_tag_ids")
    @classmethod
    def unique_knowledge_tag_ids(cls, value: list[int]) -> list[int]:
        if any(tag_id <= 0 for tag_id in value) or len(set(value)) != len(value):
            raise ValueError("지식 태그 ID 목록은 양의 정수이며 중복될 수 없습니다.")
        return value

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_rich_content(
            value,
            field_name="레슨 본문",
            allow_empty=True,
        )


class LessonUpdate(BaseModel):
    """레슨 수정 요청."""
    title: str | None = Field(None, min_length=1, max_length=200)
    content: str | None = None
    order_index: int | None = Field(None, ge=0)
    estimated_minutes: int | None = Field(None, ge=1, le=600)
    challenge_id: int | None = None
    knowledge_tag_ids: list[int] | None = Field(None, max_length=50)
    knowledge_tag_mapping: ContentTagMappingUpdate | None = None

    @field_validator("knowledge_tag_ids")
    @classmethod
    def unique_knowledge_tag_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and (any(tag_id <= 0 for tag_id in value) or len(set(value)) != len(value)):
            raise ValueError("지식 태그 ID 목록은 양의 정수이며 중복될 수 없습니다.")
        return value

    @model_validator(mode="after")
    def exclusive_mapping_input(self):
        if self.knowledge_tag_ids is not None and self.knowledge_tag_mapping is not None:
            raise ValueError("지식 태그 ID와 구조화 매핑은 동시에 지정할 수 없습니다.")
        return self

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_rich_content(
            value,
            field_name="레슨 본문",
            allow_empty=True,
        )


# ──── 응답 ────

class AttachmentInfo(BaseModel):
    """레슨 첨부파일 메타데이터 (내부 — object_path 포함)."""
    filename: str
    original_name: str
    size: int
    content_type: str
    object_path: str


class AttachmentPublic(BaseModel):
    """학습자용 첨부파일 정보 (object_path 제외)."""
    filename: str
    original_name: str
    size: int
    content_type: str


class ImageUploadResponse(BaseModel):
    """이미지 업로드 응답."""
    asset_id: UUID
    url: str
    markdown: str


class LessonSibling(BaseModel):
    """이전/다음 레슨 정보."""
    id: UUID
    title: str


class LessonSummary(BaseModel):
    id: UUID
    title: str
    order_index: int
    estimated_minutes: int
    challenge_id: int | None = None
    completed: bool = False

    model_config = {"from_attributes": True}


class LessonDetail(BaseModel):
    """관리자용 레슨 상세 (object_path 포함)."""
    id: UUID
    title: str
    content: str | None = None
    order_index: int
    estimated_minutes: int
    challenge_id: int | None = None
    attachments: list[AttachmentInfo] = []
    completed: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class LessonResponse(BaseModel):
    """학습자용 레슨 상세 응답 (siblings 포함, object_path 제외)."""
    lesson: "LessonPublic"
    siblings: dict  # {"prev": LessonSibling|null, "next": LessonSibling|null}


class LessonPublic(BaseModel):
    """학습자용 레슨 정보 (object_path 제외)."""
    id: UUID
    title: str
    content: str | None = None
    order_index: int
    estimated_minutes: int
    challenge_id: int | None = None
    attachments: list[AttachmentPublic] = []
    completed: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class CourseSummary(BaseModel):
    id: UUID
    title: str
    category: str
    difficulty: str
    estimated_hours: int
    thumbnail_url: str | None = None
    is_published: bool
    is_deleted: bool = False
    status: str = "draft"
    lesson_count: int = 0
    progress_percent: float = 0.0
    created_at: datetime

    model_config = {"from_attributes": True}


class CourseListResponse(BaseModel):
    """Opt-in 페이지 메타데이터를 포함한 코스 목록 응답."""

    items: list[CourseSummary]
    total: int
    page: int
    size: int


class CoursePrerequisiteDetail(BaseModel):
    id: UUID
    title: str
    completed: bool = False
    completed_lessons: int = 0
    total_lessons: int = 0


class CoursePrerequisiteNextAction(BaseModel):
    """잠긴 코스에서 사용자에게 제시할 공개 가능한 다음 행동."""

    kind: Literal[
        "open_course",
        "open_prerequisite_course",
        "sign_in",
        "contact_admin",
    ]
    href: str = Field(..., min_length=1, max_length=500)


class CoursePrerequisiteAccess(BaseModel):
    """코스 선행조건 판정의 설명 전용 공개 계약.

    이 객체는 저장된 선행조건 행을 그대로 노출하지 않는다. 필요한 코스와 진척은
    ``prerequisite_details``에 공개 가능 범위로만 담고, 이 객체는 잠금 원인과 다음
    행동만 전달한다.
    """

    access_state: Literal["available", "locked", "sign_in_required"]
    reason_code: Literal[
        "all_prerequisites_completed",
        "authentication_required",
        "prerequisites_incomplete",
        "required_content_unavailable",
    ]
    next_action: CoursePrerequisiteNextAction


class CourseDetail(BaseModel):
    id: UUID
    title: str
    description: str | None = None
    metadata_revision: str
    category: str
    difficulty: str
    estimated_hours: int
    thumbnail_url: str | None = None
    is_published: bool
    is_deleted: bool = False
    status: str = "draft"
    rejection_reason: str | None = None
    author_id: UUID
    lessons: list[LessonSummary] = []
    prerequisites: list[UUID] = []
    prerequisite_details: list[CoursePrerequisiteDetail] = []
    prerequisite_access: CoursePrerequisiteAccess
    locked: bool = False
    progress_percent: float = 0.0
    created_at: datetime

    model_config = {"from_attributes": True}


class CourseMetadataResponse(BaseModel):
    id: UUID
    title: str
    description: str | None = None
    metadata_revision: str


class DeletionImpactResponse(BaseModel):
    resource_type: str
    target_id: UUID
    target_name: str
    is_deleted: bool
    related_counts: dict[str, int]
    recovery_available: bool
    deleted_at: datetime | None = None


class DeletionConfirmationRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000)
    target_name: str = Field(..., min_length=1, max_length=200)


class DeletionConfirmationResponse(BaseModel):
    confirmation_token: str
    expires_at: datetime


class DeletionExecuteRequest(BaseModel):
    confirmation_token: str = Field(..., min_length=20, max_length=200)


class DeletionActionResponse(BaseModel):
    message: str
    target_id: UUID
    is_deleted: bool
    recovery_available: bool


class PendingCourseResponse(BaseModel):
    """승인 대기 코스 목록 응답."""
    items: list[CourseSummary]
    total: int
    page: int
    size: int


class ApprovalActionResponse(BaseModel):
    """승인/거부 처리 결과 응답."""
    id: UUID
    status: str
    message: str
    reviewed_by: UUID | None = None
    reviewed_at: datetime | None = None


class ProgressResponse(BaseModel):
    lesson_id: UUID
    completed: bool
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class MyProgressResponse(BaseModel):
    course_id: UUID
    course_title: str
    total_lessons: int
    completed_lessons: int
    progress_percent: float

    model_config = {"from_attributes": True}
