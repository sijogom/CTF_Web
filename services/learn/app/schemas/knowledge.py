"""지식 태그 및 학습 경로 스키마."""
from __future__ import annotations
from datetime import datetime
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.utils.knowledge_mapping_v2 import (
    KnowledgeMappingValidationError,
    V2_RELATION_TYPES,
    validate_sha256_digest,
    validate_structured_evidence,
)


_MAX_RELATION_EVIDENCE_BYTES = 8_192
_MAX_RELATION_EVIDENCE_DEPTH = 5
_MAX_RELATION_EVIDENCE_ITEMS = 50


def _validate_relation_evidence(value: dict[str, Any] | None, field_name: str):
    """관계 근거 JSON을 작고 감사 가능한 형태로 제한한다.

    관계 근거는 파일 본문·개인정보·무제한 모델 출력 저장소가 아니다. 대용량 근거는
    별도 보관소의 식별자만 저장해야 하며, 이 필드는 사람이 검토할 최소 메타데이터로
    유지한다.
    """
    if value is None:
        return None

    def visit(item: Any, depth: int) -> None:
        if depth > _MAX_RELATION_EVIDENCE_DEPTH:
            raise ValueError(f"{field_name}의 중첩 깊이는 {_MAX_RELATION_EVIDENCE_DEPTH}를 넘을 수 없습니다.")
        if isinstance(item, dict):
            if len(item) > _MAX_RELATION_EVIDENCE_ITEMS:
                raise ValueError(f"{field_name}의 객체 항목 수가 너무 많습니다.")
            for key, nested in item.items():
                if not isinstance(key, str) or not key.strip() or len(key) > 100:
                    raise ValueError(f"{field_name}의 키 형식이 올바르지 않습니다.")
                visit(nested, depth + 1)
        elif isinstance(item, list):
            if len(item) > _MAX_RELATION_EVIDENCE_ITEMS:
                raise ValueError(f"{field_name}의 배열 항목 수가 너무 많습니다.")
            for nested in item:
                visit(nested, depth + 1)
        elif isinstance(item, str):
            if len(item) > 2_000:
                raise ValueError(f"{field_name}의 문자열 항목이 너무 깁니다.")
        elif item is None or isinstance(item, (bool, int, float)):
            return
        else:
            raise ValueError(f"{field_name}에는 JSON으로 표현 가능한 값만 저장할 수 있습니다.")

    visit(value, 0)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name}은 JSON 형식이어야 합니다.") from exc
    if len(encoded.encode("utf-8")) > _MAX_RELATION_EVIDENCE_BYTES:
        raise ValueError(f"{field_name}은 {_MAX_RELATION_EVIDENCE_BYTES}바이트를 넘을 수 없습니다.")
    return value


# ── 지식 태그 ──

class KnowledgeTagCreate(BaseModel):
    name: str = Field(..., max_length=100)
    slug: str = Field(..., max_length=100, pattern=r"^[a-z0-9\-]+$")
    parent_id: int | None = None
    color: str | None = Field(None, max_length=7)
    description: str | None = None


class KnowledgeTagUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    slug: str | None = Field(None, max_length=100, pattern=r"^[a-z0-9\-]+$")
    parent_id: int | None = None
    color: str | None = Field(None, max_length=7)
    description: str | None = None


class KnowledgeTagResponse(BaseModel):
    id: int
    name: str
    slug: str
    parent_id: int | None = None
    color: str | None = None
    description: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class KnowledgeTagDeletionCounts(BaseModel):
    challenge: int = Field(ge=0)
    course: int = Field(ge=0)
    lesson: int = Field(ge=0)
    children: int = Field(ge=0)
    relations: int = Field(ge=0)
    proposals: int = Field(ge=0)
    audit_events: int = Field(ge=0)


class KnowledgeTagDeletionPreview(BaseModel):
    tag: KnowledgeTagResponse
    counts: KnowledgeTagDeletionCounts
    confirmation: str = Field(pattern=r"^[a-f0-9]{64}$")


# ── 학습 선행조건 ──

class LearningPrerequisiteItem(BaseModel):
    """API 응답용 학습 선행조건 상세."""
    type: str
    id: str
    name: str
    completed: bool = False


class PrerequisiteDetailItem(BaseModel):
    """API 응답용 챌린지 선행조건 상세."""
    id: int
    name: str
    solved: bool = False


class RecommendedPrerequisiteItem(BaseModel):
    """태그 기반 추천 선행조건."""
    type: str
    id: str
    name: str


# ── 학습 경로 그래프 ──

class GraphNode(BaseModel):
    id: str
    type: str
    name: str
    tags: list[str] = []
    status: str = "unknown"
    entity_id: str | None = None
    href: str | None = None
    course_id: str | None = None


class GraphEdge(BaseModel):
    source: str
    target: str
    type: str
    is_directed: bool = True
    strength: float = 1.0
    confidence: float = 1.0
    evidence_summary: str | None = None
    origin: str | None = None


class KnowledgeGraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


# ── 관계 원장 및 관리자 검토 ──

KnowledgeContentType = Literal[
    "challenge", "course", "lesson", "knowledge_tag", "hint", "event", "assignment"
]
KnowledgeRelationType = Literal[
    "requires", "prepares_for", "recommends", "helps", "related", "similar", "tag_affinity"
]
KnowledgeRelationOrigin = Literal[
    "manual", "rule", "model_candidate", "import", "observation"
]
KnowledgeRelationStatus = Literal[
    "draft", "pending_review", "approved", "rejected", "retired"
]

# V2 제안서는 기존 공개 관계 API와 분리되어 있다. ``prepares_for``는 V2 승인
# 공개본에서만 새로 쓰며, 자동 후보 자체는 어떤 접근 잠금도 바꾸지 않는다.
KnowledgeV2RelationType = Literal[
    "requires", "prepares_for", "recommends", "helps", "related", "similar", "tag_affinity"
]
KnowledgeProposalOrigin = Literal[
    "manual", "rule", "model_candidate", "import", "observation"
]
KnowledgeProposalState = Literal[
    "draft", "pending_review", "approved", "rejected", "retired", "superseded"
]


class KnowledgeContentRef(BaseModel):
    type: KnowledgeContentType
    id: str = Field(..., min_length=1, max_length=64)

    @field_validator("id")
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("콘텐츠 ID는 비어 있을 수 없습니다.")
        return value


class KnowledgeContentSummary(KnowledgeContentRef):
    name: str
    is_active: bool
    href: str | None = None
    course_id: str | None = None


class KnowledgeContentSearchResponse(BaseModel):
    items: list[KnowledgeContentSummary]
    total: int
    offset: int
    limit: int


class KnowledgeRelationCreate(BaseModel):
    source: KnowledgeContentRef
    target: KnowledgeContentRef
    relation_type: KnowledgeRelationType
    is_directed: bool = True
    strength: float = Field(1.0, ge=0, le=1)
    confidence: float = Field(1.0, ge=0, le=1)
    origin: KnowledgeRelationOrigin = "manual"
    evidence_summary: str | None = Field(None, max_length=1000)
    evidence: dict[str, Any] = Field(default_factory=dict)
    quality_version: str | None = Field(None, max_length=120)
    legacy_reference: dict[str, Any] | None = None

    @field_validator("evidence", "legacy_reference")
    @classmethod
    def validate_evidence_shape(cls, value: dict[str, Any] | None, info):
        return _validate_relation_evidence(value, info.field_name)

    @field_validator("evidence_summary")
    @classmethod
    def normalize_evidence_summary(cls, value: str | None) -> str | None:
        return value.strip() if value else None

    @field_validator("quality_version")
    @classmethod
    def normalize_quality_version(cls, value: str | None) -> str | None:
        return value.strip() if value else None

    @model_validator(mode="after")
    def require_reviewable_evidence(self):
        if not self.evidence_summary and not self.evidence:
            raise ValueError("관계 후보에는 검토 가능한 근거 요약 또는 구조화된 근거가 필요합니다.")
        if self.origin == "model_candidate" and not self.quality_version:
            raise ValueError("모델 후보 관계에는 생성 규칙 또는 모델 버전이 필요합니다.")
        return self


class KnowledgeRelationUpdate(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)
    source: KnowledgeContentRef | None = None
    target: KnowledgeContentRef | None = None
    relation_type: KnowledgeRelationType | None = None
    is_directed: bool | None = None
    strength: float | None = Field(None, ge=0, le=1)
    confidence: float | None = Field(None, ge=0, le=1)
    origin: KnowledgeRelationOrigin | None = None
    evidence_summary: str | None = Field(None, max_length=1000)
    evidence: dict[str, Any] | None = None
    quality_version: str | None = Field(None, max_length=120)
    legacy_reference: dict[str, Any] | None = None

    @field_validator("evidence", "legacy_reference")
    @classmethod
    def validate_evidence_shape(cls, value: dict[str, Any] | None, info):
        return _validate_relation_evidence(value, info.field_name)

    @field_validator("evidence_summary")
    @classmethod
    def normalize_evidence_summary(cls, value: str | None) -> str | None:
        return value.strip() if value else None

    @field_validator("quality_version")
    @classmethod
    def normalize_quality_version(cls, value: str | None) -> str | None:
        return value.strip() if value else None

    @field_validator("reason")
    @classmethod
    def strip_update_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("수정 사유는 3자 이상 입력해야 합니다.")
        return value


class KnowledgeRelationReviewRequest(BaseModel):
    action: Literal["approve", "reject", "retire"]
    reason: str = Field(..., min_length=3, max_length=1000)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("검토 사유는 3자 이상 입력해야 합니다.")
        return value


class KnowledgeRelationReviewResponse(BaseModel):
    id: UUID
    reviewer_id: UUID
    action: str
    reason: str | None = None
    # 일부 레거시 junction 행은 001 이전에 생성되어 시각이 없을 수 있다.
    # 새 매핑은 migration 017과 API가 반드시 기록한다.
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class KnowledgeRelationResponse(BaseModel):
    id: UUID
    source: KnowledgeContentSummary
    target: KnowledgeContentSummary
    relation_type: str
    is_directed: bool
    strength: float
    confidence: float
    origin: str
    evidence_summary: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    quality_version: str | None = None
    status: str
    created_by: UUID
    reviewed_by: UUID | None = None
    approved_at: datetime | None = None
    retired_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    reviews: list[KnowledgeRelationReviewResponse] = Field(default_factory=list)
    pending_proposal_id: UUID | None = None


class KnowledgeRelationListResponse(BaseModel):
    items: list[KnowledgeRelationResponse]
    total: int
    offset: int
    limit: int


class KnowledgeNodeReconcileRequest(BaseModel):
    dry_run: bool = True
    entity_types: list[KnowledgeContentType] | None = None

    @field_validator("entity_types")
    @classmethod
    def unique_entity_types(cls, value: list[KnowledgeContentType] | None):
        if value is not None and len(set(value)) != len(value):
            raise ValueError("같은 콘텐츠 유형을 중복해서 요청할 수 없습니다.")
        return value


class KnowledgeNodeReconcileResponse(BaseModel):
    dry_run: bool
    scanned: int
    updated: int
    deactivated: int
    missing_source: int


class ContentTagMappingItem(BaseModel):
    """하나의 콘텐츠-지식 태그 연결과 품질 메타데이터."""

    tag_id: int
    direction: Literal["content_to_knowledge_tag"] = "content_to_knowledge_tag"
    strength: float = Field(1.0, ge=0, le=1)
    confidence: float = Field(1.0, ge=0, le=1)
    evidence_summary: str = Field("수동 지식 태그 매핑", min_length=1, max_length=1000)

    @field_validator("tag_id")
    @classmethod
    def valid_tag_id(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("지식 태그 ID는 양수여야 합니다.")
        return value

    @field_validator("evidence_summary")
    @classmethod
    def valid_evidence_summary(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("근거 요약은 비워 둘 수 없습니다.")
        return normalized


class ContentTagMappingUpdate(BaseModel):
    """레거시 tag_ids와 품질 메타데이터를 포함한 mappings를 함께 지원한다."""

    # 기존 관리자 화면/클라이언트 호환용. mappings가 있으면 둘의 집합이 같아야 한다.
    tag_ids: list[int] | None = Field(default=None, max_length=50)
    mappings: list[ContentTagMappingItem] | None = Field(default=None, max_length=50)
    expected_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("tag_ids")
    @classmethod
    def valid_tag_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(tag_id <= 0 for tag_id in value):
            raise ValueError("지식 태그 ID는 양수여야 합니다.")
        if len(set(value)) != len(value):
            raise ValueError("같은 지식 태그를 중복 선택할 수 없습니다.")
        return value

    @model_validator(mode="after")
    def mappings_match_tag_ids(self) -> "ContentTagMappingUpdate":
        if self.mappings is None:
            return self
        mapping_ids = [item.tag_id for item in self.mappings]
        if len(set(mapping_ids)) != len(mapping_ids):
            raise ValueError("같은 지식 태그를 중복 매핑할 수 없습니다.")
        if self.tag_ids is not None and self.tag_ids != mapping_ids:
            raise ValueError("tag_ids와 mappings의 태그 집합이 일치해야 합니다.")
        return self

    def resolved_mappings(self) -> list[ContentTagMappingItem]:
        if self.mappings is not None:
            return self.mappings
        return [ContentTagMappingItem(tag_id=tag_id) for tag_id in self.tag_ids or []]


class ContentTagMappingEntry(BaseModel):
    """관리자 태그 연결 한 건의 provenance/품질 메타데이터."""

    tag_id: int
    direction: Literal["content_to_knowledge_tag"]
    strength: float = Field(..., ge=0, le=1)
    confidence: float = Field(..., ge=0, le=1)
    evidence_summary: str
    # ``rule`` is the deterministic auto-mapping origin.  It is an approved
    # source mapping, not an unreviewed model candidate; keeping it in the
    # response contract prevents the admin editor from failing validation when
    # an automatic backfill has created a junction row.
    origin: Literal["manual", "import", "rule", "model_candidate"]
    created_by: UUID | None = None
    # 일부 레거시 junction 행은 001 이전에 생성되어 시각이 없을 수 있다.
    # 새 매핑은 migration 017과 API가 반드시 기록한다.
    created_at: datetime | None = None
    approved_at: datetime | None = None
    source_relation_id: UUID | None = None


class ContentTagMappingResponse(BaseModel):
    entity_type: Literal["challenge", "course", "lesson"]
    entity_id: str
    tag_ids: list[int]
    mappings: list[ContentTagMappingEntry] = Field(default_factory=list)
    mapping_revision: str = ""


class KnowledgeTagUnmappedContent(BaseModel):
    """관리자가 실제 매핑 작업을 시작할 수 있는 공개 미분류 콘텐츠 한 건."""

    entity_type: Literal["challenge", "course", "lesson"]
    entity_id: str
    name: str
    href: str
    course_id: str | None = None


class KnowledgeTagReconcileResponse(BaseModel):
    """규칙 기반 공개 콘텐츠-지식 태그 자동 매핑 결과."""

    dry_run: bool
    scanned: int
    candidates: int
    created: int
    existing: int
    skipped: int
    by_entity: dict[str, int] = Field(default_factory=dict)


class ContentTagMappingAuditEventResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    entity_type: Literal["challenge", "course", "lesson"]
    entity_id: str
    tag_id: int
    action: Literal["attach", "detach"]
    actor_id: UUID
    mapping_snapshot: dict[str, Any]
    created_at: datetime


# ─── V2 후보·검토 원장 ───────────────────────────────────────────────


def _raise_mapping_validation_error(exc: KnowledgeMappingValidationError) -> ValueError:
    return ValueError(str(exc))


class KnowledgeProposalEvidenceCreate(BaseModel):
    """검토 화면에 표시할 구조화된 근거 한 건.

    ``summary``는 근거의 짧은 설명이지 콘텐츠 원문, 풀이, 플래그를 보관하는 칸이
    아니다. 실제 원본은 원본 콘텐츠 권한 경로에서만 다시 확인한다.
    """

    evidence_kind: Literal["manual", "rule", "model", "metadata", "reference"]
    source: KnowledgeContentRef | None = None
    source_digest: str | None = Field(None, max_length=64)
    locator: str | None = Field(None, max_length=500, pattern=r"^[A-Za-z0-9._:/#?=&-]+$")
    summary: str = Field(..., min_length=3, max_length=1000)
    score_component: dict[str, Any] | None = None
    generator_version: str | None = Field(None, max_length=120)

    @field_validator("source_digest")
    @classmethod
    def validate_optional_source_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return validate_sha256_digest(value, field_name="source_digest")
        except KnowledgeMappingValidationError as exc:
            raise _raise_mapping_validation_error(exc) from exc

    @field_validator("summary")
    @classmethod
    def validate_safe_evidence_summary(cls, value: str) -> str:
        try:
            # validate_structured_evidence uses the same sensitive-key/text guard for
            # nested data; a one-field object keeps summary validation equally strict.
            validate_structured_evidence({"summary": value})
        except KnowledgeMappingValidationError as exc:
            raise _raise_mapping_validation_error(exc) from exc
        return value.strip()

    @field_validator("locator")
    @classmethod
    def validate_safe_locator(cls, value: str | None) -> str | None:
        if not value:
            return None
        normalized = value.strip()
        # locator는 검토자가 원본 메타데이터 위치를 다시 찾기 위한 식별자일 뿐,
        # capability URL이나 인증정보를 저장하는 위치가 아니다. query string 전체를
        # 막아 인코딩·별칭으로 민감 키 검사를 우회하는 경로도 없앤다.
        if "?" in normalized:
            raise ValueError("locator에는 query string을 넣을 수 없습니다. #anchor 또는 안전한 원본 식별자를 사용하세요.")
        return normalized

    @field_validator("score_component")
    @classmethod
    def validate_score_component(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        try:
            return validate_structured_evidence(value)
        except KnowledgeMappingValidationError as exc:
            raise _raise_mapping_validation_error(exc) from exc

    @field_validator("generator_version")
    @classmethod
    def normalize_generator_version(cls, value: str | None) -> str | None:
        return value.strip() if value else None


class KnowledgeRelationProposalCreate(BaseModel):
    source: KnowledgeContentRef
    target: KnowledgeContentRef
    relation_type: KnowledgeV2RelationType
    is_directed: bool = True
    strength: float = Field(1.0, ge=0, le=1)
    confidence: float = Field(1.0, ge=0, le=1)
    origin: KnowledgeProposalOrigin = "manual"
    evidence_summary: str | None = Field(None, max_length=1000)
    evidence: list[KnowledgeProposalEvidenceCreate] = Field(..., min_length=1, max_length=20)
    quality_version: str | None = Field(None, max_length=120)
    base_relation_id: UUID | None = None

    @field_validator("evidence_summary")
    @classmethod
    def validate_safe_summary(cls, value: str | None) -> str | None:
        if not value:
            return None
        try:
            validate_structured_evidence({"summary": value})
        except KnowledgeMappingValidationError as exc:
            raise _raise_mapping_validation_error(exc) from exc
        return value.strip()

    @field_validator("quality_version")
    @classmethod
    def normalize_v2_quality_version(cls, value: str | None) -> str | None:
        return value.strip() if value else None

    @model_validator(mode="after")
    def validate_reviewable_proposal(self):
        if self.source.type == self.target.type and self.source.id == self.target.id:
            raise ValueError("자기 자신을 가리키는 지식 관계 제안서는 만들 수 없습니다.")
        if self.relation_type not in V2_RELATION_TYPES:
            raise ValueError("지원하지 않는 V2 관계 유형입니다.")
        if self.origin == "model_candidate" and not self.quality_version:
            raise ValueError("모델 후보 제안서에는 모델 또는 규칙 버전이 필요합니다.")
        if not self.evidence_summary and not self.evidence:
            raise ValueError("제안서에는 검토 가능한 근거 요약 또는 구조화된 근거가 필요합니다.")
        return self


class KnowledgeProposalReviewRequest(BaseModel):
    action: Literal["approve", "reject", "retire"]
    reason: str = Field(..., min_length=3, max_length=1000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 3:
            raise ValueError("검토 사유는 공백을 제외하고 3자 이상이어야 합니다.")
        return normalized


class KnowledgeProposalBulkReviewRequest(KnowledgeProposalReviewRequest):
    proposal_ids: list[UUID] = Field(..., min_length=1, max_length=50)

    @field_validator("proposal_ids")
    @classmethod
    def distinct_proposals(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("동일 제안서를 중복 검토할 수 없습니다.")
        return value


class KnowledgeProposalEvidenceResponse(BaseModel):
    id: UUID
    evidence_kind: str
    source: KnowledgeContentSummary | None = None
    source_digest: str | None = None
    locator: str | None = None
    summary: str
    score_component: dict[str, Any] | None = None
    generator_version: str | None = None
    created_at: datetime


class KnowledgeProposalReviewResponse(BaseModel):
    id: UUID
    reviewer_id: UUID | None = None
    action: str
    reason: str | None = None
    created_at: datetime


class KnowledgeRelationProposalResponse(BaseModel):
    id: UUID
    base_relation_id: UUID | None = None
    published_relation_id: UUID | None = None
    source: KnowledgeContentSummary
    target: KnowledgeContentSummary
    relation_type: str
    is_directed: bool
    strength: float
    confidence: float
    origin: str
    evidence_summary: str | None = None
    quality_version: str | None = None
    generation_run_id: UUID | None = None
    state: str
    content_revision: str
    created_by: UUID | None = None
    reviewed_by: UUID | None = None
    approved_at: datetime | None = None
    retired_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    evidence: list[KnowledgeProposalEvidenceResponse] = Field(default_factory=list)
    reviews: list[KnowledgeProposalReviewResponse] = Field(default_factory=list)


class KnowledgeProposalListResponse(BaseModel):
    items: list[KnowledgeRelationProposalResponse]
    total: int
    offset: int
    limit: int


class KnowledgeCandidateRunCreate(BaseModel):
    """후보 생성 런의 dry-run 메타데이터 등록 계약.

    실제 worker 실행은 아직 별도 서비스 계약으로 추가한다. 이 API는 실행 버전과
    입력 snapshot digest를 감사 가능하게 남기되, 후보를 자동 공개하지 않는다.
    """

    run_kind: Literal["rule", "model", "import"]
    algorithm_version: str = Field(..., min_length=1, max_length=120)
    configuration_digest: str = Field(..., min_length=64, max_length=64)
    input_snapshot_digest: str = Field(..., min_length=64, max_length=64)
    dry_run: Literal[True] = True

    @field_validator("algorithm_version")
    @classmethod
    def normalize_algorithm_version(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("algorithm_version은 비어 있을 수 없습니다.")
        return normalized

    @field_validator("configuration_digest", "input_snapshot_digest")
    @classmethod
    def validate_run_digest(cls, value: str, info) -> str:
        try:
            return validate_sha256_digest(value, field_name=info.field_name)
        except KnowledgeMappingValidationError as exc:
            raise _raise_mapping_validation_error(exc) from exc


class KnowledgeCandidateRunResponse(BaseModel):
    id: UUID
    run_kind: str
    algorithm_version: str
    configuration_digest: str
    input_snapshot_digest: str
    input_snapshot_schema_version: str = Field("legacy-unversioned", min_length=1, max_length=40)
    execution_options: dict[str, Any] = Field(default_factory=dict)
    input_snapshot: list[dict[str, Any]] = Field(default_factory=list)
    requested_max_candidates: int | None = Field(None, ge=1, le=500)
    requested_by: UUID | None = None
    trigger_kind: Literal["manual", "scheduled"] = "manual"
    execution_principal: str = "admin_api"
    attempt_count: int = Field(1, ge=1)
    state: str
    is_dry_run: bool
    started_at: datetime | None = None
    finished_at: datetime | None = None
    input_count: int
    candidate_count_before_limit: int
    preview_truncated: bool
    output_count: int
    duplicate_count: int
    error_count: int
    error_code: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class KnowledgeCandidateRunListResponse(BaseModel):
    items: list[KnowledgeCandidateRunResponse]
    total: int
    offset: int
    limit: int


class KnowledgeCandidateRunnerStatusResponse(BaseModel):
    """관리자에게만 노출하는 scheduled runner 구성/원장 상태.

    이 모델은 worker 프로세스 생존 신호가 아니라 API 컨테이너가 읽은 안전한 설정과
    마지막 scheduled 원장을 구분해 보여 준다.
    """

    configuration_valid: bool
    enabled: bool
    interval_seconds: int | None = Field(None, ge=60, le=86_400)
    max_candidates: int | None = Field(None, ge=1, le=500)
    execution_principal: str | None = Field(None, max_length=100)
    last_run: KnowledgeCandidateRunResponse | None = None


# ─── V2 공개 그래프 계약 ─────────────────────────────────────────────


class KnowledgeCandidateGenerationRequest(BaseModel):
    """서버가 고정 규칙으로 실행하는 후보 생성 요청.

    호출자는 알고리즘 버전·점수·근거·입력 digest를 전달할 수 없다. 기본 dry-run은
    미리보기와 실행 원장만 만들며, 실제 실행도 pending_review 제안만 생성한다.
    """

    dry_run: bool = True
    max_candidates: int = Field(100, ge=1, le=500)


class KnowledgeCandidatePreview(BaseModel):
    source: KnowledgeContentSummary
    target: KnowledgeContentSummary
    relation_type: Literal["prepares_for", "helps", "tag_affinity"]
    strength: float = Field(..., ge=0, le=1)
    confidence: float = Field(..., ge=0, le=1)
    evidence_summary: str
    score_component: dict[str, Any]


class KnowledgeCandidateGenerationResponse(BaseModel):
    run: KnowledgeCandidateRunResponse
    preview: list[KnowledgeCandidatePreview] = Field(default_factory=list)
    preview_truncated: bool = False


class KnowledgeMapV2Node(BaseModel):
    """원본 테이블 ID 기반의 안정적인 공개 그래프 노드.

    내부 registry UUID는 public graph identifier로 노출하지 않는다. 이를 통해 노드
    동기화 시점이 달라도 `type:id` 기준 URL과 UI 상태를 안정적으로 유지한다.
    """

    id: str
    type: str
    entity_id: str
    name: str
    href: str | None = None
    is_focus: bool = False
    distance: int = Field(0, ge=0, le=3)


class KnowledgeMapV2Edge(BaseModel):
    id: UUID
    source: str
    target: str
    relation_type: str
    is_directed: bool
    strength: float = Field(..., ge=0, le=1)
    confidence: float = Field(..., ge=0, le=1)
    evidence_summary: str | None = None
    origin: str
    created_at: datetime | None = None
    approved_at: datetime | None = None


class KnowledgeMapV2FocusResponse(BaseModel):
    graph_revision: str
    focus: KnowledgeMapV2Node
    nodes: list[KnowledgeMapV2Node]
    edges: list[KnowledgeMapV2Edge]
    layout_hints: dict[str, Any]
    truncated: bool = False
    omitted_counts: dict[str, int] = Field(default_factory=dict)
    continuation: str | None = None
    next_cursor: str | None = None
    applied_filters: dict[str, Any] = Field(default_factory=dict)


class KnowledgeMapV2CanvasResponse(BaseModel):
    """전체 공개 지식 맵을 위한 bounded canvas 응답.

    ``focus`` API는 한 콘텐츠 주변의 bounded BFS를 제공하고, 이 응답은
    전체 공개 projection을 한 번에 그릴 때 사용한다. 두 응답은 동일한
    노드·엣지·layout 힌트 계약을 공유하지만 전체 캔버스에는 중심 focus가
    없다.
    """

    graph_revision: str
    nodes: list[KnowledgeMapV2Node]
    edges: list[KnowledgeMapV2Edge]
    layout_hints: dict[str, Any]
    truncated: bool = False
    omitted_counts: dict[str, int] = Field(default_factory=dict)
    next_cursor: str | None = None
    applied_filters: dict[str, Any] = Field(default_factory=dict)


class KnowledgeMapV2MappingCoverage(BaseModel):
    """공개 콘텐츠가 실제 지식 태그 매핑을 갖는지 보여 주는 품질 지표."""

    public_content: dict[str, int] = Field(default_factory=dict)
    tagged_content: dict[str, int] = Field(default_factory=dict)
    unmapped_content: dict[str, int] = Field(default_factory=dict)
    coverage_percent: dict[str, float] = Field(default_factory=dict)


class KnowledgeMapV2OverviewResponse(BaseModel):
    graph_revision: str
    node_counts: dict[str, int]
    edge_counts: dict[str, int]
    relation_count: int
    # Deterministic public seed so the map is useful before a search is entered.
    default_focus: KnowledgeMapV2Node | None = None
    mapping_coverage: KnowledgeMapV2MappingCoverage = Field(default_factory=KnowledgeMapV2MappingCoverage)


class KnowledgeMapV2SearchResult(BaseModel):
    type: str
    entity_id: str
    id: str
    name: str
    href: str | None = None


class KnowledgeMapV2SearchResponse(BaseModel):
    query: str
    items: list[KnowledgeMapV2SearchResult]
    total: int
    limit: int


class RecommendationConsentResponse(BaseModel):
    enabled: bool
    scope: Literal["learning_recommendations"] = "learning_recommendations"
    policy_version: str
    granted_at: datetime | None = None
    withdrawn_at: datetime | None = None


class RecommendationConsentUpdate(BaseModel):
    enabled: bool


class RecommendationEvidence(BaseModel):
    source_type: Literal["course", "lesson", "challenge"]
    source_id: str
    source_name: str
    relation_type: Literal["prepares_for", "recommends", "helps"]
    strength: float = Field(..., ge=0, le=1)
    confidence: float = Field(..., ge=0, le=1)
    evidence_summary: str | None = None


class RecommendationItem(BaseModel):
    type: Literal["course", "lesson", "challenge"]
    id: str
    name: str
    href: str
    score: float = Field(..., ge=0, le=1)
    reason_codes: list[Literal["prepares_for", "recommends", "helps"]] = Field(default_factory=list)
    evidence: list[RecommendationEvidence] = Field(default_factory=list)
    is_locked: bool = False
    prerequisites: list["RecommendationAction"] = Field(default_factory=list)
    next_action: "RecommendationAction | None" = None


class RecommendationAction(BaseModel):
    type: Literal["course", "lesson", "challenge"]
    id: str
    name: str
    href: str
    completed: bool = False


class RecommendationResponse(BaseModel):
    consent_required: bool
    enabled: bool
    policy_version: str
    graph_revision: str | None = None
    generated_at: datetime
    items: list[RecommendationItem] = Field(default_factory=list)
    empty_reason: Literal["no_history", "no_candidates"] | None = None
