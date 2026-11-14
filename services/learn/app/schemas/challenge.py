"""챌린지 관련 Pydantic 스키마."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.schemas.challenge_attachment import AttachmentFields, ChallengeAttachmentResponse
from app.schemas.knowledge import ContentTagMappingUpdate

from app.utils.dynamic_flags import (
    DYNAMIC_FLAG_RUNTIME_PATH,
    DYNAMIC_FLAG_TYPE,
    MAX_DYNAMIC_FLAG_TTL_SECONDS,
    MIN_DYNAMIC_FLAG_TTL_SECONDS,
    validate_dynamic_key_version,
    validate_dynamic_runtime_path,
)
from app.utils.safe_html import normalize_rich_content_mapping, sanitize_rich_content


# ──── 요청 ────

class LearningContentReference(BaseModel):
    """챌린지에서 참조하는 학습 콘텐츠의 타입 안전한 식별자."""

    type: Literal["course", "lesson"]
    target_id: UUID


class LearningPathGraphFocus(BaseModel):
    """A public graph focus reference returned with each path step."""

    focus_type: Literal["challenge", "course", "lesson", "knowledge_tag", "hint"]
    focus_id: str = Field(..., min_length=1, max_length=64)


class LearningPathStep(BaseModel):
    """A visible required or recommended learning-path step."""

    type: Literal["challenge", "course", "lesson"]
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    status: Literal[
        "completed",
        "in_progress",
        "not_started",
        "unavailable",
        "unknown",
    ]
    href: str = Field(..., min_length=1, max_length=500)
    graph_focus: LearningPathGraphFocus
    completed_units: int | None = Field(None, ge=0)
    total_units: int | None = Field(None, ge=0)
    course_id: str | None = Field(None, min_length=1, max_length=64)


class LearningPathHelpfulContent(BaseModel):
    """Public, approved supporting content with safe relation metadata only."""

    type: Literal["challenge", "course", "lesson", "knowledge_tag", "hint"]
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    status: Literal[
        "completed",
        "in_progress",
        "not_started",
        "unavailable",
        "unknown",
    ]
    href: str = Field(..., min_length=1, max_length=500)
    graph_focus: LearningPathGraphFocus
    course_id: str | None = Field(None, min_length=1, max_length=64)
    relation_type: Literal["prepares_for", "recommends", "helps"]
    strength: float = Field(..., ge=0, le=1)
    confidence: float = Field(..., ge=0, le=1)
    evidence_summary: str | None = Field(None, max_length=1000)
    origin: str = Field(..., min_length=1, max_length=64)


class LearningPathTarget(BaseModel):
    type: Literal["challenge"] = "challenge"
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    href: str = Field(..., min_length=1, max_length=500)


class LearningPathNextAction(BaseModel):
    kind: Literal["open_challenge", "open_content", "sign_in", "contact_admin"]
    href: str = Field(..., min_length=1, max_length=500)


class LearningPathChallengeResponse(BaseModel):
    """Read-only explanation of a public wargame challenge access state."""

    target: LearningPathTarget
    access_state: Literal["available", "locked", "sign_in_required"]
    reason_code: Literal[
        "all_required_steps_completed",
        "authentication_required",
        "required_steps_incomplete",
        "required_content_unavailable",
    ]
    required_steps: list[LearningPathStep] = Field(default_factory=list)
    recommended_steps: list[LearningPathStep] = Field(default_factory=list)
    helpful_content: list[LearningPathHelpfulContent] = Field(default_factory=list)
    next_action: LearningPathNextAction
    map_focus_href: str = Field(..., min_length=1, max_length=500)


def _unique_learning_references(
    items: list[LearningContentReference],
    *,
    field_name: str,
) -> list[LearningContentReference]:
    seen: set[tuple[str, UUID]] = set()
    for item in items:
        key = (item.type, item.target_id)
        if key in seen:
            raise ValueError(f"{field_name}에 같은 학습 콘텐츠를 중복 지정할 수 없습니다.")
        seen.add(key)
    return items


class DynamicFlagPolicyInput(BaseModel):
    """관리자가 설정하는 dynamic_instance의 비밀 없는 전달 정책.

    HMAC key material이나 raw flag는 이 요청에 포함하지 않는다. key는 서버의 secret
    설정으로만 관리하고, 이 객체는 version 식별자만 보관한다.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    delivery: str = Field("file", pattern=r"^file$")
    runtime_path: str = Field(DYNAMIC_FLAG_RUNTIME_PATH, max_length=256)
    ttl_seconds: int = Field(
        3600,
        ge=MIN_DYNAMIC_FLAG_TTL_SECONDS,
        le=MAX_DYNAMIC_FLAG_TTL_SECONDS,
    )
    hmac_key_version: str = Field("v1", min_length=1, max_length=64)

    @field_validator("runtime_path")
    @classmethod
    def validate_runtime_path(cls, value: str) -> str:
        try:
            return validate_dynamic_runtime_path(value)
        except ValueError as error:
            raise ValueError(str(error)) from error

    @field_validator("hmac_key_version")
    @classmethod
    def validate_key_version(cls, value: str) -> str:
        try:
            return validate_dynamic_key_version(value)
        except ValueError as error:
            raise ValueError(str(error)) from error

class ChallengeCreate(AttachmentFields):
    """챌린지 생성 요청."""
    name: str = Field(..., min_length=1, max_length=200)
    category: str = Field("web", pattern=r"^(web|crypto|pwn|rev|forensic|misc)$")
    difficulty: str = Field("beginner", pattern=r"^(beginner|easy|medium_low|medium|medium_high|hard|expert|impossible)$")
    description: str = ""
    value: int = Field(100, ge=0, le=10000)
    xp: int = Field(50, ge=0, le=5000)
    max_attempts: int = Field(0, ge=0, description="0 = 무제한")
    flag_type: str = Field("static", pattern=r"^(static|regex|dynamic_instance)$")
    flag_value: str = Field("", max_length=500)
    flags: list["FlagCreate"] = Field(default_factory=list)
    dynamic_flag: DynamicFlagPolicyInput | None = None
    tags: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    env_type: str = Field("none", pattern=r"^(none|dockerfile|image)$")
    dockerfile_content: str = Field("", max_length=50000)
    docker_image: str = Field("", max_length=500)
    container_port: int = Field(8080, ge=1, le=65535)
    hints: list["HintCreate"] = Field(default_factory=list)
    prerequisite_ids: list[int] = Field(default_factory=list, description="선행조건 챌린지 ID 목록")
    knowledge_tag_ids: list[int] = Field(default_factory=list, description="지식 태그 ID 목록")
    learning_prerequisites: list[LearningContentReference] = Field(
        default_factory=list,
        description='학습 선행조건 [{"type":"course"|"lesson","target_id":"uuid"}]',
    )
    recommended_challenges: list[int] = Field(default_factory=list, description="추천 선행 챌린지 ID 목록")
    recommended_learning: list[LearningContentReference] = Field(
        default_factory=list,
        description='추천 선행 학습 [{"type":"course"|"lesson","target_id":"uuid"}]',
    )
    challenge_type: str = Field("wargame", pattern=r"^(wargame|competition)$")
    thumbnail_url: str | None = None
    is_published: bool = False

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        # 문제 본문은 TipTap HTML을 저장한다. 기존 Markdown 문제는 읽기
        # 호환하되, 새 HTML은 공통 안전 태그/URL 정책을 거친다.
        return sanitize_rich_content(
            value,
            field_name="문제 설명",
            allow_empty=True,
            max_visible_chars=10000,
        )

    @model_validator(mode="after")
    def check_flag_mode(self):
        if self.flag_type == DYNAMIC_FLAG_TYPE:
            if self.dynamic_flag is None or not self.dynamic_flag.enabled:
                raise ValueError("dynamic_instance 플래그에는 활성화된 dynamic_flag 정책이 필요합니다.")
            if self.env_type == "none":
                raise ValueError("dynamic_instance 플래그에는 Docker 실습 환경이 필요합니다.")
            if self.flag_value.strip() or self.flags:
                raise ValueError("dynamic_instance 플래그에는 정적 flag_value 또는 flags를 지정할 수 없습니다.")
            return self

        if self.dynamic_flag is not None:
            raise ValueError("dynamic_flag 정책은 dynamic_instance 플래그에서만 사용할 수 있습니다.")
        # flags 배열이 있으면 개별 검증으로 위임
        if self.flags:
            return self
        if self.flag_type == "static" and not self.flag_value.strip():
            raise ValueError("static 플래그는 빈 값일 수 없습니다.")
        return self

    @field_validator("learning_prerequisites", "recommended_learning")
    @classmethod
    def unique_learning_references(cls, value, info):
        return _unique_learning_references(value, field_name=info.field_name)

    @field_validator("prerequisite_ids")
    @classmethod
    def unique_prerequisite_ids(cls, value: list[int]) -> list[int]:
        if any(challenge_id <= 0 for challenge_id in value):
            raise ValueError("선행 문제 ID는 양수여야 합니다.")
        if len(set(value)) != len(value):
            raise ValueError("같은 선행 문제를 중복 지정할 수 없습니다.")
        return value


class FlagCreate(BaseModel):
    """플래그 생성 요청 — 복수 플래그 + regex 지원."""
    type: str = Field("static", pattern=r"^(static|regex)$")
    value: str = Field(..., min_length=1, max_length=500)
    case_sensitive: bool = True


class HintCreate(BaseModel):
    """힌트 생성 요청."""
    content: str = Field(..., min_length=1)
    cost: int = Field(0, ge=0, le=1000)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        # 새 관리자 편집기는 TipTap HTML을 저장한다. 기존 Markdown 힌트는
        # 이미 저장된 문제와 외부 호출 호환을 위해 읽기·수정 시 유지한다.
        return sanitize_rich_content(
            value,
            field_name="힌트",
            max_visible_chars=2000,
        )


class ChallengeUpdate(AttachmentFields):
    """챌린지 수정 요청 (부분 업데이트)."""
    name: str | None = Field(None, min_length=1, max_length=200)
    name_i18n: dict[str, str] | None = None
    category: str | None = Field(None, pattern=r"^(web|crypto|pwn|rev|forensic|misc)$")
    difficulty: str | None = Field(None, pattern=r"^(beginner|easy|medium_low|medium|medium_high|hard|expert|impossible)$")
    description: str | None = None
    description_i18n: dict[str, str] | None = None
    value: int | None = Field(None, ge=0, le=10000)
    xp: int | None = Field(None, ge=0, le=5000)
    max_attempts: int | None = Field(None, ge=0)
    flag_type: str | None = Field(None, pattern=r"^(static|regex|dynamic_instance)$")
    flag_value: str | None = Field(None, max_length=500)
    flags: list["FlagCreate"] | None = None
    dynamic_flag: DynamicFlagPolicyInput | None = None
    tags: list[str] | None = None
    files: list[str] | None = None
    env_type: str | None = Field(None, pattern=r"^(none|dockerfile|image)$")
    dockerfile_content: str | None = Field(None, max_length=50000)
    docker_image: str | None = Field(None, max_length=500)
    container_port: int | None = Field(None, ge=1, le=65535)
    hints: list["HintCreate"] | None = None
    prerequisite_ids: list[int] | None = None
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
    learning_prerequisites: list[LearningContentReference] | None = None
    recommended_challenges: list[int] | None = None
    recommended_learning: list[LearningContentReference] | None = None

    @field_validator("learning_prerequisites", "recommended_learning")
    @classmethod
    def unique_learning_references(cls, value, info):
        if value is None:
            return value
        return _unique_learning_references(value, field_name=info.field_name)

    @field_validator("prerequisite_ids")
    @classmethod
    def unique_prerequisite_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return value
        if any(challenge_id <= 0 for challenge_id in value):
            raise ValueError("선행 문제 ID는 양수여야 합니다.")
        if len(set(value)) != len(value):
            raise ValueError("같은 선행 문제를 중복 지정할 수 없습니다.")
        return value
    challenge_type: str | None = Field(None, pattern=r"^(wargame|competition)$")
    thumbnail_url: str | None = None
    is_published: bool | None = None

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_rich_content(
            value,
            field_name="문제 설명",
            allow_empty=True,
            max_visible_chars=10000,
        )

    @field_validator("description_i18n")
    @classmethod
    def validate_description_i18n(
        cls,
        value: dict[str, str] | None,
    ) -> dict[str, str] | None:
        if value is None:
            return None
        return normalize_rich_content_mapping(
            value,
            field_name="문제 설명 번역",
            allow_empty=True,
            max_visible_chars=10000,
        )


class FlagSubmit(BaseModel):
    """플래그 제출 요청."""
    flag: str = Field(..., min_length=1, max_length=500)
    competition_id: str | None = None  # 대회 컨텍스트에서 제출 시


# ──── 응답 ────

class ChallengeListItem(BaseModel):
    """챌린지 목록 항목 (상세 제외)."""
    id: int
    name: str
    category: str
    difficulty: str = "beginner"
    value: int = 100
    xp: int = 0
    solves: int = 0
    tags: list[str] = []
    author: str | None = None
    challenge_type: str = "wargame"
    env_type: str = "none"
    thumbnail_url: str | None = None
    is_published: bool = True
    prerequisite_ids: list[int] = []
    locked: bool = False
    solved: bool = False
    knowledge_tags: list[str] = []
    learning_prerequisites_detail: list[dict] = []
    prerequisite_details: list[dict] = []
    recommended_prerequisites: list[dict] = []

    model_config = {"from_attributes": True}


class HintItem(BaseModel):
    """힌트 메타 (내용은 언락 전까지 숨김)."""
    id: int
    cost: int

    model_config = {"from_attributes": True}


class ChallengeDetailResponse(BaseModel):
    """챌린지 상세 응답."""
    id: int
    name: str
    category: str
    difficulty: str = "beginner"
    value: int = 100
    solves: int = 0
    tags: list[str] = []
    author: str | None = None
    description: str = ""
    files: list[str] = []
    attachment_revision: int = 0
    attachments: list[ChallengeAttachmentResponse] = Field(default_factory=list)
    hints: list[HintItem] = []
    max_attempts: int = 0
    env_type: str = "none"
    container_port: int = 8080
    thumbnail_url: str | None = None

    model_config = {"from_attributes": True}


class ChallengeListResponse(BaseModel):
    """챌린지 목록 응답 (페이지네이션)."""
    items: list[ChallengeListItem] = []
    total: int = 0
    page: int = 1
    size: int = 20


class SubmitResponse(BaseModel):
    """플래그 제출 응답."""
    status: str   # "correct" | "incorrect" | "already_solved" | "score_sync_pending"
    message: str


class HintUnlockResponse(BaseModel):
    """힌트 언락 응답."""
    content: str
