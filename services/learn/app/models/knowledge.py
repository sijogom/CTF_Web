"""보안 지식 태그 및 학습 선행조건 모델."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, Numeric, String,
    Text, UniqueConstraint, CheckConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.models.course import Base


class KnowledgeTag(Base):
    """보안 지식 분류 태그."""
    __tablename__ = "knowledge_tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    slug = Column(String(100), unique=True, nullable=False)
    parent_id = Column(Integer, ForeignKey("knowledge_tags.id", ondelete="SET NULL"), nullable=True)
    color = Column(String(7), nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    parent = relationship("KnowledgeTag", remote_side=[id], lazy="select")
    challenge_tags = relationship("ChallengeKnowledgeTag", back_populates="tag", cascade="all, delete-orphan")
    course_tags = relationship("CourseKnowledgeTag", back_populates="tag", cascade="all, delete-orphan")
    lesson_tags = relationship("LessonKnowledgeTag", back_populates="tag", cascade="all, delete-orphan")


class ChallengeKnowledgeTag(Base):
    """챌린지-지식태그 연결."""
    __tablename__ = "challenge_knowledge_tags"
    __table_args__ = (
        CheckConstraint(
            "direction = 'content_to_knowledge_tag'",
            name="ck_challenge_knowledge_tag_direction",
        ),
        CheckConstraint(
            "strength >= 0 AND strength <= 1",
            name="ck_challenge_knowledge_tag_strength",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_challenge_knowledge_tag_confidence",
        ),
        CheckConstraint(
            "origin IN ('manual', 'import', 'rule', 'model_candidate')",
            name="ck_challenge_knowledge_tag_origin",
        ),
    )

    challenge_id = Column(Integer, ForeignKey("challenges.id", ondelete="CASCADE"), primary_key=True)
    tag_id = Column(Integer, ForeignKey("knowledge_tags.id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    direction = Column(String(32), nullable=False, default="content_to_knowledge_tag", server_default="content_to_knowledge_tag")
    strength = Column(Numeric(4, 3), nullable=False, default=1, server_default="1")
    confidence = Column(Numeric(4, 3), nullable=False, default=1, server_default="1")
    evidence_summary = Column(Text, nullable=False, default="수동 지식 태그 매핑", server_default="수동 지식 태그 매핑")
    origin = Column(String(24), nullable=False, default="manual", server_default="manual")
    created_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    source_relation_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_relations.id", ondelete="SET NULL"), nullable=True, index=True)

    tag = relationship("KnowledgeTag", back_populates="challenge_tags")


class CourseKnowledgeTag(Base):
    """코스-지식태그 연결."""
    __tablename__ = "course_knowledge_tags"
    __table_args__ = (
        CheckConstraint(
            "direction = 'content_to_knowledge_tag'",
            name="ck_course_knowledge_tag_direction",
        ),
        CheckConstraint(
            "strength >= 0 AND strength <= 1",
            name="ck_course_knowledge_tag_strength",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_course_knowledge_tag_confidence",
        ),
        CheckConstraint(
            "origin IN ('manual', 'import', 'rule', 'model_candidate')",
            name="ck_course_knowledge_tag_origin",
        ),
    )

    course_id = Column(UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True)
    tag_id = Column(Integer, ForeignKey("knowledge_tags.id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    direction = Column(String(32), nullable=False, default="content_to_knowledge_tag", server_default="content_to_knowledge_tag")
    strength = Column(Numeric(4, 3), nullable=False, default=1, server_default="1")
    confidence = Column(Numeric(4, 3), nullable=False, default=1, server_default="1")
    evidence_summary = Column(Text, nullable=False, default="수동 지식 태그 매핑", server_default="수동 지식 태그 매핑")
    origin = Column(String(24), nullable=False, default="manual", server_default="manual")
    created_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    source_relation_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_relations.id", ondelete="SET NULL"), nullable=True, index=True)

    tag = relationship("KnowledgeTag", back_populates="course_tags")


class LessonKnowledgeTag(Base):
    """레슨-지식태그 연결."""
    __tablename__ = "lesson_knowledge_tags"
    __table_args__ = (
        CheckConstraint(
            "direction = 'content_to_knowledge_tag'",
            name="ck_lesson_knowledge_tag_direction",
        ),
        CheckConstraint(
            "strength >= 0 AND strength <= 1",
            name="ck_lesson_knowledge_tag_strength",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_lesson_knowledge_tag_confidence",
        ),
        CheckConstraint(
            "origin IN ('manual', 'import', 'rule', 'model_candidate')",
            name="ck_lesson_knowledge_tag_origin",
        ),
    )

    lesson_id = Column(UUID(as_uuid=True), ForeignKey("lessons.id", ondelete="CASCADE"), primary_key=True)
    tag_id = Column(Integer, ForeignKey("knowledge_tags.id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    direction = Column(String(32), nullable=False, default="content_to_knowledge_tag", server_default="content_to_knowledge_tag")
    strength = Column(Numeric(4, 3), nullable=False, default=1, server_default="1")
    confidence = Column(Numeric(4, 3), nullable=False, default=1, server_default="1")
    evidence_summary = Column(Text, nullable=False, default="수동 지식 태그 매핑", server_default="수동 지식 태그 매핑")
    origin = Column(String(24), nullable=False, default="manual", server_default="manual")
    created_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    source_relation_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_relations.id", ondelete="SET NULL"), nullable=True, index=True)

    tag = relationship("KnowledgeTag", back_populates="lesson_tags")


class KnowledgeTagMappingAuditEvent(Base):
    """콘텐츠-태그 매핑 교체의 append-only 감사 원장."""

    __tablename__ = "knowledge_tag_mapping_audit_events"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('challenge', 'course', 'lesson')",
            name="ck_knowledge_tag_mapping_audit_entity_type",
        ),
        CheckConstraint(
            "action IN ('attach', 'detach')",
            name="ck_knowledge_tag_mapping_audit_action",
        ),
        Index(
            "ix_knowledge_tag_mapping_audit_entity_created",
            "entity_type",
            "entity_id",
            "created_at",
        ),
        Index(
            "ix_knowledge_tag_mapping_audit_actor_created",
            "actor_id",
            "created_at",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_type = Column(String(16), nullable=False)
    entity_id = Column(String(64), nullable=False)
    # 삭제된 태그의 원래 ID도 감사 기록에 남긴다. 현재 태그와 FK로 묶지 않는다.
    tag_id = Column(Integer, nullable=False)
    action = Column(String(16), nullable=False)
    actor_id = Column(UUID(as_uuid=True), nullable=False)
    mapping_snapshot = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class KnowledgeTagDeletionArchive(Base):
    """태그 삭제 당시 원본과 영향을 보존하는 독립 감사 기록."""

    __tablename__ = "knowledge_tag_deletion_archives"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tag_id = Column(Integer, nullable=False, index=True)
    actor_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    tag_snapshot = Column(JSONB, nullable=False)
    impact_snapshot = Column(JSONB, nullable=False)
    graph_revision = Column(UUID(as_uuid=True), nullable=False)
    deleted_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class ChallengeLearningPrerequisite(Base):
    """챌린지의 학습 선행조건 (코스/레슨 완료 필수)."""
    __tablename__ = "challenge_learning_prerequisites"
    __table_args__ = (
        UniqueConstraint(
            "challenge_id", "prerequisite_type", "prerequisite_target_id",
            name="uq_challenge_learning_prereq",
        ),
        CheckConstraint(
            "prerequisite_type IN ('course', 'lesson')",
            name="ck_learning_prereq_type",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    challenge_id = Column(Integer, ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False, index=True)
    prerequisite_type = Column(String(10), nullable=False)
    prerequisite_target_id = Column(UUID(as_uuid=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ChallengeRecommendedPrerequisite(Base):
    """챌린지의 추천 선행조건 (강제 아님, '이걸 먼저 하면 더 쉽습니다')."""
    __tablename__ = "challenge_recommended_prerequisites"
    __table_args__ = (
        UniqueConstraint(
            "challenge_id", "rec_type", "rec_target_id",
            name="uq_challenge_recommended_prereq",
        ),
        CheckConstraint(
            "rec_type IN ('challenge', 'course', 'lesson')",
            name="ck_recommended_prereq_type",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    challenge_id = Column(Integer, ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False, index=True)
    rec_type = Column(String(10), nullable=False)  # "challenge" | "course" | "lesson"
    rec_target_id = Column(String(36), nullable=False)  # challenge ID(int as str) or UUID as str
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class KnowledgeContentNode(Base):
    """관계 그래프에서 원본 콘텐츠를 안정적으로 참조하는 노드 레지스트리.

    원본 콘텐츠 테이블은 정수/UUID 등 서로 다른 ID 타입을 사용하므로, 관계 자체는
    이 레지스트리를 통해 정규화한다. ``is_active``는 조회 최적화를 위한 투영값이며,
    공개 API는 원본 콘텐츠 상태도 다시 확인해 오래된 투영으로 비공개 콘텐츠가
    노출되지 않도록 한다.
    """

    __tablename__ = "knowledge_content_nodes"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", name="uq_knowledge_content_node_entity"),
        CheckConstraint(
            "entity_type IN ('challenge', 'course', 'lesson', 'knowledge_tag', 'hint', 'event', 'assignment')",
            name="ck_knowledge_content_node_type",
        ),
        Index("ix_knowledge_content_nodes_type_active", "entity_type", "is_active"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_type = Column(String(24), nullable=False)
    entity_id = Column(String(64), nullable=False)
    display_name = Column(String(200), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    source_updated_at = Column(DateTime(timezone=True), nullable=True)
    content_digest = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class KnowledgeRelation(Base):
    """관리자 검토가 필요한 보안 학습 콘텐츠 간 관계 원장."""

    __tablename__ = "knowledge_relations"
    __table_args__ = (
        CheckConstraint("source_node_id <> target_node_id", name="ck_knowledge_relation_no_self_ref"),
        CheckConstraint(
            "relation_type IN ('requires', 'prepares_for', 'recommends', 'helps', 'related', 'similar', 'tag_affinity')",
            name="ck_knowledge_relation_type",
        ),
        CheckConstraint("strength >= 0 AND strength <= 1", name="ck_knowledge_relation_strength"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_knowledge_relation_confidence"),
        CheckConstraint(
            "origin IN ('manual', 'rule', 'model_candidate', 'import', 'observation')",
            name="ck_knowledge_relation_origin",
        ),
        CheckConstraint(
            "status IN ('draft', 'pending_review', 'approved', 'rejected', 'retired')",
            name="ck_knowledge_relation_status",
        ),
        Index("ix_knowledge_relations_source_status", "source_node_id", "status"),
        Index("ix_knowledge_relations_target_status", "target_node_id", "status"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_content_nodes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_content_nodes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    relation_type = Column(String(24), nullable=False)
    is_directed = Column(Boolean, nullable=False, default=True)
    strength = Column(Numeric(4, 3), nullable=False, default=1)
    confidence = Column(Numeric(4, 3), nullable=False, default=1)
    origin = Column(String(24), nullable=False, default="manual")
    evidence_summary = Column(Text, nullable=True)
    evidence = Column(JSONB, nullable=False, default=dict, server_default="{}")
    quality_version = Column(String(120), nullable=True)
    status = Column(String(24), nullable=False, default="pending_review", index=True)
    created_by = Column(UUID(as_uuid=True), nullable=False, index=True)
    reviewed_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    retired_at = Column(DateTime(timezone=True), nullable=True)
    legacy_reference = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    source_node = relationship("KnowledgeContentNode", foreign_keys=[source_node_id])
    target_node = relationship("KnowledgeContentNode", foreign_keys=[target_node_id])
    reviews = relationship(
        "KnowledgeRelationReview",
        back_populates="relation",
        cascade="all, delete-orphan",
        order_by="KnowledgeRelationReview.created_at",
    )


# 거절/은퇴된 과거 이력은 보존하되, 공개 후보 상태의 중복 관계는 막는다.
Index(
    "uq_knowledge_relation_active_pair",
    KnowledgeRelation.source_node_id,
    KnowledgeRelation.target_node_id,
    KnowledgeRelation.relation_type,
    KnowledgeRelation.is_directed,
    unique=True,
    postgresql_where=text("status IN ('draft', 'pending_review', 'approved')"),
)


class KnowledgeRelationReview(Base):
    """관계 생성·수정·승인·반려·은퇴 이력을 append-only로 보존한다."""

    __tablename__ = "knowledge_relation_reviews"
    __table_args__ = (
        CheckConstraint(
            "action IN ('submitted', 'edited', 'approved', 'rejected', 'retired')",
            name="ck_knowledge_relation_review_action",
        ),
        Index("ix_knowledge_relation_reviews_relation_created", "relation_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    relation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_relations.id", ondelete="CASCADE"),
        nullable=False,
    )
    reviewer_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    action = Column(String(24), nullable=False)
    reason = Column(Text, nullable=True)
    before_snapshot = Column(JSONB, nullable=True)
    after_snapshot = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    relation = relationship("KnowledgeRelation", back_populates="reviews")


# V2는 기존 ``knowledge_relations`` 공개 원장을 지우거나 후보 테이블로 바꾸지
# 않는다. 아래 모델은 후보·검토·공개 투영 이벤트를 별도로 보관하고, 승인 시에만
# 기존 공개 원장에 새 현재 버전을 만든다. 따라서 후보 작업자가 관계 그래프나 잠금
# 규칙을 직접 바꾸지 못한다.


class KnowledgeContentProfile(Base):
    """자동 후보 입력에 허용된 콘텐츠 메타데이터의 불변 스냅샷.

    ``allowed_profile``에는 제목, 교육 목표, 승인 태그처럼 allowlist로 정제된
    메타데이터만 저장한다. 풀이 원문, 플래그, 비공개 첨부파일, 제출·개인정보는 이
    모델의 계약상 저장 대상이 아니다.
    """

    __tablename__ = "knowledge_content_profiles"
    __table_args__ = (
        UniqueConstraint("node_id", "content_revision", name="uq_knowledge_content_profile_revision"),
        Index("ix_knowledge_content_profiles_node_generated", "node_id", "generated_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_content_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content_revision = Column(String(64), nullable=False)
    profile_schema_version = Column(String(40), nullable=False)
    allowed_profile = Column(JSONB, nullable=False, default=dict, server_default="{}")
    profile_digest = Column(String(64), nullable=False)
    generated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    node = relationship("KnowledgeContentNode")


class KnowledgeCandidateRun(Base):
    """후보 생성의 재현 가능한 실행 원장.

    실행 원장 자체는 공개 관계를 만들지 않는다. 실제 후보 작업자는 이 행과
    ``KnowledgeCandidateRunItem``을 남긴 뒤에도 ``pending_review`` 제안서만 만들
    수 있으며, 승인 권한을 갖지 않는다.
    """

    __tablename__ = "knowledge_candidate_runs"
    __table_args__ = (
        CheckConstraint("run_kind IN ('rule', 'model', 'import')", name="ck_knowledge_candidate_run_kind"),
        CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_knowledge_candidate_run_state",
        ),
        CheckConstraint(
            "input_count >= 0 AND candidate_count_before_limit >= 0 AND output_count >= 0 AND duplicate_count >= 0 AND error_count >= 0",
            name="ck_knowledge_candidate_run_counts",
        ),
        CheckConstraint(
            "requested_max_candidates IS NULL OR (requested_max_candidates >= 1 AND requested_max_candidates <= 500)",
            name="ck_knowledge_candidate_run_requested_max_candidates",
        ),
        CheckConstraint(
            "trigger_kind IN ('manual', 'scheduled')",
            name="ck_knowledge_candidate_run_trigger_kind",
        ),
        CheckConstraint("attempt_count >= 1", name="ck_knowledge_candidate_run_attempt_count"),
        Index("ix_knowledge_candidate_runs_state_created", "state", "created_at"),
        Index("ix_knowledge_candidate_runs_requested_created", "requested_by", "created_at"),
        Index("ix_knowledge_candidate_runs_trigger_created", "trigger_kind", "created_at"),
        Index(
            "uq_knowledge_candidate_runs_deduplication_key",
            "deduplication_key",
            unique=True,
            postgresql_where=text("deduplication_key IS NOT NULL"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_kind = Column(String(24), nullable=False)
    algorithm_version = Column(String(120), nullable=False)
    configuration_digest = Column(String(64), nullable=False)
    input_snapshot_digest = Column(String(64), nullable=False)
    # ``legacy-unversioned``은 012 이전 실행 행의 실제 형식을 과장하지 않기 위한
    # 보존 표기다. 새 실행은 candidate-input-profile-node-v2만 사용한다.
    input_snapshot_schema_version = Column(
        String(40),
        nullable=False,
        default="legacy-unversioned",
        server_default=text("'legacy-unversioned'"),
    )
    execution_options = Column(JSONB, nullable=False, default=dict, server_default="{}")
    input_snapshot = Column(JSONB, nullable=False, default=list, server_default="[]")
    # JSON 실행 옵션만으로는 감사/쿼리에서 요청 상한을 안정적으로 확인하기 어렵다.
    # 예전 dry-run 등록 행은 실제 생성 요청이 아니므로 NULL을 보존한다.
    requested_max_candidates = Column(Integer, nullable=True)
    # scheduled runner는 DB 사용자 계정을 가정하지 않으므로 requested_by를 비워 둔다.
    # 수동 API는 기존처럼 관리자 UUID를 계속 기록한다.
    requested_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    trigger_kind = Column(String(24), nullable=False, default="manual", server_default="manual")
    execution_principal = Column(
        String(100),
        nullable=False,
        default="admin_api",
        server_default="admin_api",
    )
    deduplication_key = Column(String(64), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=1, server_default="1")
    state = Column(String(24), nullable=False, default="queued", index=True)
    is_dry_run = Column(Boolean, nullable=False, default=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    input_count = Column(Integer, nullable=False, default=0)
    candidate_count_before_limit = Column(Integer, nullable=False, default=0)
    preview_truncated = Column(Boolean, nullable=False, default=False)
    output_count = Column(Integer, nullable=False, default=0)
    duplicate_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)
    error_code = Column(String(80), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class KnowledgeRelationProposal(Base):
    """신규 관계 또는 공개 관계 개정안.

    승인 전에는 이 테이블의 행만 바뀐다. ``published_relation_id``는 승인으로
    만들어진 기존 공개 원장 행을 가리키며, 공개 그래프는 이 제안서를 직접 읽지
    않는다.
    """

    __tablename__ = "knowledge_relation_proposals"
    __table_args__ = (
        CheckConstraint("source_node_id <> target_node_id", name="ck_knowledge_relation_proposal_no_self_ref"),
        CheckConstraint(
            "relation_type IN ('requires', 'prepares_for', 'recommends', 'helps', 'related', 'similar', 'tag_affinity')",
            name="ck_knowledge_relation_proposal_type",
        ),
        CheckConstraint("strength >= 0 AND strength <= 1", name="ck_knowledge_relation_proposal_strength"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_knowledge_relation_proposal_confidence"),
        CheckConstraint(
            "origin IN ('manual', 'rule', 'model_candidate', 'import', 'observation')",
            name="ck_knowledge_relation_proposal_origin",
        ),
        CheckConstraint(
            "state IN ('draft', 'pending_review', 'approved', 'rejected', 'retired', 'superseded')",
            name="ck_knowledge_relation_proposal_state",
        ),
        Index("ix_knowledge_relation_proposals_state_created", "state", "created_at"),
        Index("ix_knowledge_relation_proposals_source_state", "source_node_id", "state"),
        Index("ix_knowledge_relation_proposals_target_state", "target_node_id", "state"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    base_relation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_relations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    published_relation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_relations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_content_nodes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_content_nodes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_profile_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_content_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_profile_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_content_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    relation_type = Column(String(24), nullable=False)
    is_directed = Column(Boolean, nullable=False, default=True)
    strength = Column(Numeric(4, 3), nullable=False, default=1)
    confidence = Column(Numeric(4, 3), nullable=False, default=1)
    origin = Column(String(24), nullable=False, default="manual")
    evidence_summary = Column(Text, nullable=True)
    quality_version = Column(String(120), nullable=True)
    generation_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_candidate_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    state = Column(String(24), nullable=False, default="pending_review", index=True)
    content_revision = Column(String(64), nullable=False)
    created_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    reviewed_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    retired_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    source_node = relationship("KnowledgeContentNode", foreign_keys=[source_node_id])
    target_node = relationship("KnowledgeContentNode", foreign_keys=[target_node_id])
    source_profile = relationship("KnowledgeContentProfile", foreign_keys=[source_profile_id])
    target_profile = relationship("KnowledgeContentProfile", foreign_keys=[target_profile_id])
    evidence_items = relationship(
        "KnowledgeRelationProposalEvidence",
        back_populates="proposal",
        cascade="all, delete-orphan",
        order_by="KnowledgeRelationProposalEvidence.created_at",
    )
    reviews = relationship(
        "KnowledgeRelationProposalReview",
        back_populates="proposal",
        cascade="all, delete-orphan",
        order_by="KnowledgeRelationProposalReview.created_at",
    )


Index(
    "uq_knowledge_relation_proposal_active_pair",
    KnowledgeRelationProposal.source_node_id,
    KnowledgeRelationProposal.target_node_id,
    KnowledgeRelationProposal.relation_type,
    KnowledgeRelationProposal.is_directed,
    unique=True,
    postgresql_where=text("state IN ('draft', 'pending_review')"),
)


class KnowledgeRelationProposalEvidence(Base):
    """제안서 검토에 필요한 구조화된 최소 근거.

    원본 텍스트를 복제하지 않고 source digest, locator, 요약과 점수 구성만 보관한다.
    """

    __tablename__ = "knowledge_relation_proposal_evidence"
    __table_args__ = (
        CheckConstraint(
            "evidence_kind IN ('manual', 'rule', 'model', 'metadata', 'reference')",
            name="ck_knowledge_relation_proposal_evidence_kind",
        ),
        Index("ix_knowledge_relation_proposal_evidence_proposal_created", "proposal_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposal_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_relation_proposals.id", ondelete="CASCADE"),
        nullable=False,
    )
    evidence_kind = Column(String(24), nullable=False)
    source_node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_content_nodes.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_digest = Column(String(64), nullable=True)
    locator = Column(String(500), nullable=True)
    summary = Column(String(1000), nullable=False)
    score_component = Column(JSONB, nullable=True)
    generator_version = Column(String(120), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    proposal = relationship("KnowledgeRelationProposal", back_populates="evidence_items")
    source_node = relationship("KnowledgeContentNode")


class KnowledgeRelationProposalReview(Base):
    """V2 제안서 상태 전이의 append-only 감사 원장."""

    __tablename__ = "knowledge_relation_proposal_reviews"
    __table_args__ = (
        CheckConstraint(
            "action IN ('submitted', 'approved', 'rejected', 'retired', 'superseded')",
            name="ck_knowledge_relation_proposal_review_action",
        ),
        Index("ix_knowledge_relation_proposal_reviews_proposal_created", "proposal_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposal_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_relation_proposals.id", ondelete="CASCADE"),
        nullable=False,
    )
    # scheduled 후보 제출은 DB 사용자로 위장하지 않는다. 관리자 검토는 계속 UUID를 기록한다.
    reviewer_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    action = Column(String(24), nullable=False)
    reason = Column(Text, nullable=True)
    before_snapshot = Column(JSONB, nullable=True)
    after_snapshot = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    proposal = relationship("KnowledgeRelationProposal", back_populates="reviews")


class KnowledgeCandidateRunItem(Base):
    """한 후보 생성 실행의 후보 identity·제안서 연결·중복 처리 결과.

    ``proposal_id``는 실제 pending proposal이 만들어진 경우에만 존재한다. 건너뛴
    후보도 안전한 identity snapshot으로 남겨 실행 결과와 미리보기를 대조할 수 있다.
    """

    __tablename__ = "knowledge_candidate_run_items"
    __table_args__ = (
        UniqueConstraint("run_id", "proposal_id", name="uq_knowledge_candidate_run_item"),
        CheckConstraint("rank >= 0", name="ck_knowledge_candidate_run_item_rank"),
        CheckConstraint("raw_score >= 0 AND raw_score <= 1", name="ck_knowledge_candidate_run_item_score"),
        CheckConstraint(
            "dedupe_outcome IN ('created', 'existing_skipped', 'rejected_precheck', 'preview_only')",
            name="ck_knowledge_candidate_run_item_dedupe",
        ),
        Index("ix_knowledge_candidate_run_items_run_rank", "run_id", "rank"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_candidate_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    proposal_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_relation_proposals.id", ondelete="RESTRICT"),
        nullable=True,
    )
    rank = Column(Integer, nullable=False)
    raw_score = Column(Numeric(4, 3), nullable=False)
    dedupe_outcome = Column(String(32), nullable=False)
    candidate_snapshot = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class KnowledgeProjectionOutbox(Base):
    """공개 관계 변경을 캐시/선택적 Graph DB 투영으로 전달하는 단일 outbox."""

    __tablename__ = "knowledge_projection_outbox"
    __table_args__ = (
        CheckConstraint(
            "aggregate_type IN ('knowledge_relation', 'knowledge_relation_proposal', 'knowledge_tag')",
            name="ck_knowledge_projection_outbox_aggregate",
        ),
        CheckConstraint(
            "event_type IN ('proposal_approved', 'proposal_rejected', 'proposal_retired', "
            "'relation_published', 'relation_retired', 'tag_deleted')",
            name="ck_knowledge_projection_outbox_event",
        ),
        Index("ix_knowledge_projection_outbox_processed_created", "processed_at", "created_at"),
        Index("ix_knowledge_projection_outbox_aggregate", "aggregate_type", "aggregate_id", "created_at"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    aggregate_type = Column(String(40), nullable=False)
    aggregate_id = Column(UUID(as_uuid=True), nullable=False)
    event_type = Column(String(40), nullable=False)
    graph_revision = Column(UUID(as_uuid=True), nullable=False, default=uuid.uuid4, index=True)
    payload = Column(JSONB, nullable=False, default=dict, server_default="{}")
    payload_digest = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)


class KnowledgeGraphRevision(Base):
    """관련 원본 변경 TX의 MVCC 원장. 전역 counter 행을 잠그지 않는다."""
    __tablename__ = "knowledge_graph_revision"
    __table_args__ = (UniqueConstraint("revision", name="uq_knowledge_graph_revision_transaction"),)
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    revision = Column(BigInteger, nullable=False)


class RecommendationConsent(Base):
    """사용자별 개인화 추천 동의의 현재 상태.

    사용자 원장은 Auth 서비스에 있으므로 Learn DB에는 교차 데이터베이스 FK를
    만들지 않는다. 상태 변경 이력은 ``RecommendationConsentEvent``에 append-only
    로 남겨 철회 이후에도 동의 판단을 재구성할 수 있게 한다.
    """

    __tablename__ = "recommendation_consents"
    __table_args__ = (
        UniqueConstraint("user_id", "scope", name="uq_recommendation_consent_user_scope"),
        CheckConstraint(
            "scope = 'learning_recommendations'",
            name="ck_recommendation_consent_scope",
        ),
        Index("ix_recommendation_consents_user_enabled", "user_id", "enabled"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    scope = Column(String(64), nullable=False, default="learning_recommendations", server_default="learning_recommendations")
    policy_version = Column(String(64), nullable=False, default="learning-recommendations-v1", server_default="learning-recommendations-v1")
    enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    granted_at = Column(DateTime(timezone=True), nullable=True)
    withdrawn_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    events = relationship(
        "RecommendationConsentEvent",
        back_populates="consent",
        cascade="all, delete-orphan",
        order_by="RecommendationConsentEvent.created_at",
    )


class RecommendationConsentEvent(Base):
    """개인화 추천 동의/철회 append-only 이력."""

    __tablename__ = "recommendation_consent_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('enabled', 'disabled')",
            name="ck_recommendation_consent_event_action",
        ),
        Index("ix_recommendation_consent_events_user_created", "user_id", "created_at"),
        Index("ix_recommendation_consent_events_consent_created", "consent_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    consent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("recommendation_consents.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    action = Column(String(16), nullable=False)
    policy_version = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    consent = relationship("RecommendationConsent", back_populates="events")
