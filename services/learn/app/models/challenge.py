"""챌린지(워게임 문제) 관련 DB 모델."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint, Column, Index, String, Integer, Boolean, DateTime, Text, LargeBinary,
    ForeignKey, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from app.models.course import Base


class Challenge(Base):
    """워게임 챌린지(문제) 테이블."""
    __tablename__ = "challenges"
    __table_args__ = (
        CheckConstraint(
            "category IN ('web','crypto','pwn','rev','forensic','misc')",
            name="ck_challenges_category",
        ),
        CheckConstraint(
            "difficulty IN ('beginner','easy','medium_low','medium','medium_high','hard','expert','impossible')",
            name="ck_challenges_difficulty",
        ),
        CheckConstraint("value >= 0", name="ck_challenges_value_positive"),
        CheckConstraint(
            "challenge_type IN ('wargame','competition')",
            name="ck_challenges_type",
        ),
        CheckConstraint(
            "flag_type IN ('static','regex','dynamic_instance')",
            name="ck_challenges_flag_type",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    name_i18n = Column(JSONB, nullable=False, default=dict)         # {"en": "...", "ko": "...", ...}
    category = Column(String(50), nullable=False, index=True)
    difficulty = Column(String(20), nullable=False, default="beginner", index=True)
    description = Column(Text, nullable=False, default="")
    description_i18n = Column(JSONB, nullable=False, default=dict)  # {"en": "...", "ko": "...", ...}
    value = Column(Integer, nullable=False, default=100)          # 포인트
    xp = Column(Integer, nullable=False, default=50)              # 경험치
    solves = Column(Integer, nullable=False, default=0)           # 풀이 수
    max_attempts = Column(Integer, nullable=False, default=0)     # 0 = 무제한
    # static/regex는 기존 정답 저장 방식, dynamic_instance는 별도 policy+issuance
    # 테이블로만 검증한다. raw dynamic flag는 이 행에 저장하지 않는다.
    flag_type = Column(String(20), nullable=False, default="static")
    flag_value = Column(String(500), nullable=False, default="")      # 정답 플래그 (static)
    tags = Column(JSONB, nullable=False, default=list)            # ["web", "sql"]
    files = Column(JSONB, nullable=False, default=list)           # ["url1", "url2"]
    attachment_revision = Column(Integer, nullable=False, default=0, server_default="0")
    author_id = Column(UUID(as_uuid=True), nullable=True)         # 출제자 (크로스 DB)
    author_name = Column(String(100), nullable=True)              # 출제자 이름 (조회 편의)
    env_type = Column(String(20), nullable=False, default="none")          # none, dockerfile, image
    dockerfile_content = Column(Text, nullable=False, default="")            # Dockerfile 내용
    docker_image = Column(String(500), nullable=False, default="")           # Docker 이미지 이름
    container_port = Column(Integer, nullable=False, default=8080, server_default="8080")  # 컨테이너 내부 포트
    # 환경 입력 또는 빌드 컨텍스트가 달라질 때마다 증가한다. 같은 revision을 실제로
    # 준비 확인한 기록만 공개·인스턴스 생성에 사용할 수 있다.
    environment_revision = Column(Integer, nullable=False, default=1, server_default="1")
    environment_verified_revision = Column(Integer, nullable=True)
    environment_verified_at = Column(DateTime(timezone=True), nullable=True)
    environment_verified_by = Column(UUID(as_uuid=True), nullable=True)
    environment_verified_image_id = Column(String(128), nullable=True)
    environment_last_checked_at = Column(DateTime(timezone=True), nullable=True)
    environment_last_failure_code = Column(String(64), nullable=True)
    challenge_type = Column(String(20), nullable=False, default="wargame", server_default="wargame", index=True)  # wargame | competition
    thumbnail_url = Column(String(500), nullable=True)  # 문제 대표 이미지
    is_published = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    hints = relationship("Hint", back_populates="challenge", order_by="Hint.order_index")
    flags = relationship("Flag", back_populates="challenge", order_by="Flag.order_index")
    submissions = relationship("Submission", back_populates="challenge")
    dynamic_flag_policy = relationship(
        "ChallengeFlagPolicy",
        back_populates="challenge",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    dynamic_flag_issuances = relationship(
        "DynamicFlagIssuance",
        back_populates="challenge",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ChallengeEnvironmentVerification(Base):
    """문제 환경 준비 확인의 append-only 실행 원장.

    Docker 로그, private network IP, 컨테이너 ID, 플래그는 저장하지 않는다. 문제를
    삭제하는 기존 정책과 정합되도록 원장은 해당 문제 삭제 시에만 함께 제거된다.
    """

    __tablename__ = "challenge_environment_verifications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ready', 'failed')",
            name="ck_challenge_environment_verification_status",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_challenge_environment_verification_revision",
        ),
        Index(
            "ix_challenge_environment_verifications_challenge_checked",
            "challenge_id",
            "checked_at",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    challenge_id = Column(
        Integer,
        ForeignKey("challenges.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False)
    failure_code = Column(String(64), nullable=True)
    image_reference = Column(String(500), nullable=False)
    # 실패는 Docker image inspect 전에도 발생할 수 있다. 준비 완료 기록만 image ID를
    # 반드시 가져야 하며, 실패 원장은 빈 식별자 때문에 사라지지 않도록 nullable이다.
    image_id = Column(String(128), nullable=True)
    verified_by = Column(UUID(as_uuid=True), nullable=False)
    checked_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class Hint(Base):
    """챌린지 힌트 테이블."""
    __tablename__ = "hints"

    id = Column(Integer, primary_key=True, autoincrement=True)
    challenge_id = Column(Integer, ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False, index=True)
    content = Column(Text, nullable=False)       # 힌트 내용
    cost = Column(Integer, nullable=False, default=0)  # 포인트 차감 비용
    order_index = Column(Integer, nullable=False, default=0)

    challenge = relationship("Challenge", back_populates="hints")


class Flag(Base):
    """챌린지 플래그 테이블 — 복수 플래그 + regex 지원."""
    __tablename__ = "flags"
    __table_args__ = (
        CheckConstraint(
            "type IN ('static','regex')",
            name="ck_flags_type",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    challenge_id = Column(Integer, ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False, index=True)
    type = Column(String(10), nullable=False, default="static")  # static, regex
    value = Column(String(500), nullable=False)
    case_sensitive = Column(Boolean, nullable=False, default=True)
    order_index = Column(Integer, nullable=False, default=0)

    challenge = relationship("Challenge", back_populates="flags")


class ChallengeFlagPolicy(Base):
    """동적 인스턴스 플래그의 비밀 없는 정책.

    정책은 dynamic_instance challenge에만 존재한다. HMAC key material이나 raw flag는
    어떤 컬럼에도 저장하지 않고 key version만 보관한다.
    """

    __tablename__ = "challenge_flag_policies"
    __table_args__ = (
        CheckConstraint(
            "mode = 'dynamic_instance'",
            name="ck_challenge_flag_policy_mode",
        ),
        CheckConstraint(
            "delivery = 'file'",
            name="ck_challenge_flag_policy_delivery",
        ),
        CheckConstraint(
            "runtime_path = '/flag.txt'",
            name="ck_challenge_flag_policy_runtime_path",
        ),
        CheckConstraint(
            "policy_revision >= 1",
            name="ck_challenge_flag_policy_revision",
        ),
        CheckConstraint(
            "ttl_seconds BETWEEN 60 AND 86400",
            name="ck_challenge_flag_policy_ttl",
        ),
    )

    challenge_id = Column(
        Integer,
        ForeignKey("challenges.id", ondelete="CASCADE"),
        primary_key=True,
    )
    mode = Column(String(32), nullable=False, default="dynamic_instance")
    delivery = Column(String(16), nullable=False, default="file")
    runtime_path = Column(String(256), nullable=False, default="/flag.txt")
    ttl_seconds = Column(Integer, nullable=False, default=3600)
    policy_revision = Column(Integer, nullable=False, default=1)
    hmac_key_version = Column(String(64), nullable=False, default="v1")
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    challenge = relationship("Challenge", back_populates="dynamic_flag_policy")


class DynamicFlagIssuance(Base):
    """인스턴스별 dynamic flag 발급 원장.

    `token_hmac`은 raw token이 아닌 HMAC-SHA-256 digest다. `instance_locator`는 DB
    조회를 하나의 row로 좁히는 공개 식별자일 뿐 정답 비밀이 아니다.
    """

    __tablename__ = "dynamic_flag_issuances"
    __table_args__ = (
        CheckConstraint("environment_revision >= 1", name="ck_dynamic_flag_issuance_environment_revision"),
        CheckConstraint("policy_revision >= 1", name="ck_dynamic_flag_issuance_policy_revision"),
        CheckConstraint("expires_at > issued_at", name="ck_dynamic_flag_issuance_expiry"),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= issued_at",
            name="ck_dynamic_flag_issuance_revoked_at",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= issued_at",
            name="ck_dynamic_flag_issuance_consumed_at",
        ),
        CheckConstraint(
            "octet_length(token_hmac) = 32",
            name="ck_dynamic_flag_issuance_token_hmac_length",
        ),
        Index(
            "uq_dynamic_flag_issuance_locator",
            "instance_locator",
            unique=True,
        ),
        Index(
            "ix_dynamic_flag_issuances_instance_active",
            "instance_id",
            unique=True,
            # 만료된 issuance는 재발급 전에도 active unique slot을 차지하면 안 된다.
            # PostgreSQL partial index predicate에는 현재 시각 함수를 쓸 수 없으므로
            # cleanup/재발급이 만료 row를 revoke해 slot을 해제한다.
            postgresql_where=text("revoked_at IS NULL AND consumed_at IS NULL"),
        ),
        Index(
            "ix_dynamic_flag_issuances_challenge_owner_context_expiry",
            "challenge_id",
            "user_id",
            "competition_id",
            "expires_at",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instance_locator = Column(String(64), nullable=False)
    # 재발급 시 과거 row의 audit를 보존하기 위해 전체 unique가 아닌 active partial
    # unique index를 사용한다. 만료 row도 재발급 시 명시 revoke된다.
    instance_id = Column(String(128), nullable=False, index=True)
    challenge_id = Column(
        Integer,
        ForeignKey("challenges.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    competition_id = Column(String(36), nullable=True, index=True)
    environment_revision = Column(Integer, nullable=False)
    verified_image_id = Column(String(128), nullable=False)
    policy_revision = Column(Integer, nullable=False)
    hmac_key_version = Column(String(64), nullable=False)
    token_hmac = Column(LargeBinary(32), nullable=False)
    issued_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revocation_reason = Column(String(64), nullable=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)

    challenge = relationship("Challenge", back_populates="dynamic_flag_issuances")


class Submission(Base):
    """플래그 제출 기록 테이블."""
    __tablename__ = "submissions"
    # 보존 경계: 레거시 static/regex 제출은 ``submitted_flag``에 평문을 남길 수
    # 있다. 이 과거 값에는 아직 승인된 저장 암호화·삭제 보존 계약이 없다.
    # 전용 마이그레이션이 승인될 때까지 API 응답, 감사 UI, 진단 payload, 애플리케이션
    # 로그에 원문을 반환하지 않고, 감사 화면에는 비가역 메타데이터만 제공한다.
    __table_args__ = (
        # NULL은 여러 행에 허용하되, 소비된 dynamic issuance 하나가 두 정답 제출에
        # 연결되는 것은 DB에서도 막는다.
        Index(
            "uq_submissions_dynamic_flag_issuance",
            "dynamic_flag_issuance_id",
            unique=True,
        ),
        CheckConstraint(
            "submitted_flag_length >= 0",
            name="ck_submissions_submitted_flag_length_nonnegative",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    challenge_id = Column(Integer, ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False, index=True)
    submitted_flag = Column(String(500), nullable=False)
    # 신규 제출은 원문 대신 고정 마커·길이·선택적 HMAC 지문을 이 컬럼에 남긴다.
    # 레거시 행은 migration 이후에도 submitted_flag에 평문이 남아 있을 수 있으므로
    # storage version으로 보존 경계를 식별하고, admin-only reveal에서만 호환한다.
    submitted_flag_length = Column(Integer, nullable=False, default=0, server_default="0")
    submitted_flag_fingerprint = Column(LargeBinary(length=32), nullable=True)
    submitted_flag_storage_version = Column(
        String(40),
        nullable=False,
        default="legacy-raw",
        server_default="legacy-raw",
    )
    # 신규 제출 원문은 Fernet 암호문으로만 보관한다. 평문은 관리자 reveal
    # endpoint가 복호화하는 순간 외에는 API·로그·목록 응답에 나타나지 않는다.
    submitted_flag_ciphertext = Column(LargeBinary, nullable=True)
    submitted_flag_key_version = Column(String(64), nullable=True)
    is_correct = Column(Boolean, nullable=False, default=False)
    reward_pending = Column(Boolean, nullable=False, default=False)
    competition_id = Column(String(36), nullable=True, index=True)  # 대회 컨텍스트 ID
    # dynamic_instance 제출은 fixed marker만 submitted_flag에 저장하며, 이 FK는
    # 어떤 issuance가 원자적으로 소비됐는지 보존한다. raw dynamic flag는 저장하지 않는다.
    dynamic_flag_issuance_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "dynamic_flag_issuances.id",
            name="fk_submissions_dynamic_flag_issuance",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    submitted_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    challenge = relationship("Challenge", back_populates="submissions")


class SubmissionFlagRevealAudit(Base):
    """관리자 제출 원문 열람의 append-only 감사 원장."""

    __tablename__ = "submission_flag_reveal_audits"
    __table_args__ = (
        Index("ix_submission_flag_reveal_audits_submission_viewed", "submission_id", "viewed_at"),
        Index("ix_submission_flag_reveal_audits_actor_viewed", "actor_id", "viewed_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submission_id = Column(
        UUID(as_uuid=True),
        ForeignKey("submissions.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_id = Column(UUID(as_uuid=True), nullable=False)
    # 이전 감사 행의 미수집 사유는 만들어 채우지 않는다. 새 열람은 요청 검증으로 필수화한다.
    reason = Column(Text, nullable=True)
    viewed_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


# 한 유저가 한 챌린지를 중복 정답 처리하지 않도록 부분 유니크 인덱스
# COALESCE(competition_id, '__wargame__') — 워게임(NULL)과 각 대회별로 독립 정답 기록
# PostgreSQL에서 NULL은 유니크 제약에서 구분되지 않으므로 COALESCE 필수
Index(
    "ix_unique_correct_submission",
    Submission.user_id,
    Submission.challenge_id,
    text("COALESCE(competition_id, '__wargame__')"),
    unique=True,
    postgresql_where=(Submission.is_correct.is_(True)),
)


class ChallengePrerequisite(Base):
    """챌린지 선행조건 테이블 — challenge_id를 풀려면 prerequisite_id를 먼저 풀어야 함."""
    __tablename__ = "challenge_prerequisites"
    __table_args__ = (
        CheckConstraint(
            "challenge_id != prerequisite_id",
            name="ck_no_self_prerequisite",
        ),
    )

    challenge_id = Column(
        Integer,
        ForeignKey("challenges.id", ondelete="CASCADE"),
        primary_key=True,
    )
    prerequisite_id = Column(
        Integer,
        ForeignKey("challenges.id", ondelete="CASCADE"),
        primary_key=True,
    )


class HintUnlock(Base):
    """힌트 해금 기록 테이블."""
    __tablename__ = "hint_unlocks"
    __table_args__ = (
        UniqueConstraint("user_id", "hint_id", name="uq_user_hint_unlock"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    hint_id = Column(Integer, ForeignKey("hints.id", ondelete="CASCADE"), nullable=False, index=True)
    unlocked_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
