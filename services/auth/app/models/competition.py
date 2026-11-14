import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Integer, Boolean, DateTime, Text, ForeignKey,
    CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY

from app.models.user import Base


class Competition(Base):
    __tablename__ = "competitions"
    __table_args__ = (
        CheckConstraint(
            "team_contract IS NULL OR (team_contract = 'fixed_roster_v1' AND mode = 'team')",
            name="ck_competitions_team_contract",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    mode = Column(String(20), nullable=False, default="individual")  # individual, team
    team_contract = Column(String(30), nullable=True)
    start_at = Column(DateTime(timezone=True), nullable=False)
    end_at = Column(DateTime(timezone=True), nullable=False)
    freeze_minutes = Column(Integer, default=30, nullable=False)
    banner_url = Column(String(500), nullable=True)
    difficulty_distribution = Column(JSONB, default=lambda: {"easy": 40, "medium": 35, "hard": 25})
    is_published = Column(Boolean, default=False, nullable=False)
    # 대회 기록·점수·배정은 보존하고 공개/접근만 중단한다. 물리 삭제는 별도 보존
    # 정책과 마이그레이션을 거친 운영 작업으로만 허용한다.
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by = Column(UUID(as_uuid=True), nullable=True, index=True)
    deletion_reason = Column(Text, nullable=True)
    published_before_deletion = Column(Boolean, default=False, nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    ctfd_event_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class CompetitionDeletionConfirmation(Base):
    """대회 보관 삭제의 3단계 확인 토큰.

    토큰 원문은 응답 직후 클라이언트가 최종 삭제 요청에만 사용하며 DB에는 해시만
    저장한다. 한 번 사용하거나 만료되면 재사용할 수 없다.
    """

    __tablename__ = "competition_deletion_confirmations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    competition_id = Column(
        UUID(as_uuid=True),
        ForeignKey("competitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    reason = Column(Text, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class CompetitionChallenge(Base):
    __tablename__ = "competition_challenges"

    competition_id = Column(UUID(as_uuid=True), ForeignKey("competitions.id", ondelete="CASCADE"), primary_key=True)
    challenge_id = Column(Integer, nullable=False, primary_key=True)  # CTFd challenge ID
    category = Column(String(50), nullable=False)
    difficulty = Column(String(20), nullable=False)
    # Dynamic Value scoring
    scoring_type = Column(String(10), nullable=False, default="static", server_default="static")
    initial_value = Column(Integer, nullable=False, default=500, server_default="500")
    decay = Column(Integer, nullable=False, default=100, server_default="100")
    minimum_value = Column(Integer, nullable=False, default=50, server_default="50")


class CompetitionAssignment(Base):
    __tablename__ = "competition_assignments"

    competition_id = Column(UUID(as_uuid=True), ForeignKey("competitions.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    category = Column(String(50), nullable=False, primary_key=True)
    challenge_ids = Column(ARRAY(Integer), nullable=False)
    assigned_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class CompetitionSolve(Base):
    """대회 내 챌린지 풀이 기록."""
    __tablename__ = "competition_solves"

    competition_id = Column(UUID(as_uuid=True), ForeignKey("competitions.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    challenge_id = Column(Integer, nullable=False, primary_key=True)
    solved_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class CompetitionParticipant(Base):
    __tablename__ = "competition_participants"

    competition_id = Column(UUID(as_uuid=True), ForeignKey("competitions.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    joined_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    score = Column(Integer, default=0, nullable=False)


class CompetitionTeamRegistration(Base):
    """전역 팀의 삭제·변경과 무관하게 보존하는 대회 등록 스냅샷."""

    __tablename__ = "competition_team_registrations"

    competition_id = Column(UUID(as_uuid=True), ForeignKey("competitions.id", ondelete="CASCADE"), primary_key=True)
    team_id = Column(UUID(as_uuid=True), primary_key=True)
    team_name_snapshot = Column(String(100), nullable=False)
    registered_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    registered_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class CompetitionTeamMember(Base):
    __tablename__ = "competition_team_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["competition_id", "team_id"],
            ["competition_team_registrations.competition_id", "competition_team_registrations.team_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("competition_id", "user_id", name="uq_competition_team_members_competition_user"),
    )

    competition_id = Column(UUID(as_uuid=True), primary_key=True)
    team_id = Column(UUID(as_uuid=True), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    username_snapshot = Column(String(50), nullable=False)


class CompetitionTeamAssignment(Base):
    __tablename__ = "competition_team_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["competition_id", "team_id"],
            ["competition_team_registrations.competition_id", "competition_team_registrations.team_id"],
            ondelete="CASCADE",
        ),
    )

    competition_id = Column(UUID(as_uuid=True), primary_key=True)
    team_id = Column(UUID(as_uuid=True), primary_key=True)
    category = Column(String(50), nullable=False, primary_key=True)
    challenge_ids = Column(ARRAY(Integer), nullable=False)
    assigned_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class CompetitionTeamSolve(Base):
    __tablename__ = "competition_team_solves"
    __table_args__ = (
        ForeignKeyConstraint(
            ["competition_id", "team_id"],
            ["competition_team_registrations.competition_id", "competition_team_registrations.team_id"],
            ondelete="CASCADE",
        ),
        Index("ix_competition_team_solves_competition_time", "competition_id", "solved_at"),
    )

    competition_id = Column(UUID(as_uuid=True), primary_key=True)
    team_id = Column(UUID(as_uuid=True), primary_key=True)
    challenge_id = Column(Integer, nullable=False, primary_key=True)
    solved_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    first_solver_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
