"""고정 명단 팀전 원장. 트랜잭션 종료와 HTTP 인가는 호출자가 담당한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import secrets
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import (
    Competition, CompetitionChallenge, CompetitionSolve, CompetitionTeamAssignment,
    CompetitionTeamMember, CompetitionTeamRegistration, CompetitionTeamSolve,
)
from app.models.team import Team, TeamMember
from app.models.user import User


FIXED_ROSTER_CONTRACT = "fixed_roster_v1"


class CompetitionTeamError(Exception):
    def __init__(self, status_code: int, code: str, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class TeamContext:
    registration: CompetitionTeamRegistration
    member: CompetitionTeamMember
    members: tuple[CompetitionTeamMember, ...]

    @property
    def team_id(self) -> UUID:
        return self.registration.team_id

    @property
    def team_name(self) -> str:
        return self.registration.team_name_snapshot


@dataclass(frozen=True)
class TeamAssignmentResult:
    assignment: CompetitionTeamAssignment
    already_assigned: bool


@dataclass(frozen=True)
class TeamSolveResult:
    team_id: UUID
    personal_inserted: bool
    team_inserted: bool
    earliest_changed: bool
    solve: CompetitionTeamSolve


@dataclass(frozen=True)
class TeamScoringRows:
    registrations: tuple[CompetitionTeamRegistration, ...]
    members: tuple[CompetitionTeamMember, ...]
    solves: tuple[CompetitionTeamSolve, ...]


def is_fixed_roster_team(comp: Competition) -> bool:
    return comp.mode == "team" and comp.team_contract == FIXED_ROSTER_CONTRACT


async def _competition(db: AsyncSession, competition_id: UUID, *, lock: bool) -> Competition:
    query = select(Competition).where(Competition.id == competition_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    comp = (await db.execute(query)).scalar_one_or_none()
    if comp is None or comp.is_deleted:
        raise CompetitionTeamError(404, "competition_not_found", "대회를 찾을 수 없습니다.")
    if not is_fixed_roster_team(comp):
        raise CompetitionTeamError(409, "fixed_roster_required", "고정 명단 팀전 대회가 아닙니다.")
    return comp


async def lock_team_for_membership(db: AsyncSession, team_id: UUID) -> Team:
    team = (await db.execute(select(Team).where(Team.id == team_id).with_for_update()
                            .execution_options(populate_existing=True))).scalar_one_or_none()
    if team is None:
        raise CompetitionTeamError(404, "team_not_found", "팀을 찾을 수 없습니다.")
    return team


async def register_team(
    db: AsyncSession, competition_id: UUID, team_id: UUID, actor_id: UUID,
) -> CompetitionTeamRegistration:
    await _competition(db, competition_id, lock=True)
    existing = (await db.execute(select(CompetitionTeamRegistration).where(
        CompetitionTeamRegistration.competition_id == competition_id,
        CompetitionTeamRegistration.team_id == team_id,
    ))).scalar_one_or_none()
    if existing is not None:
        if existing.registered_by != actor_id:
            raise CompetitionTeamError(403, "registration_actor_required", "최초 등록한 팀장만 재요청할 수 있습니다.")
        return existing

    team = await lock_team_for_membership(db, team_id)
    if team.captain_id != actor_id:
        raise CompetitionTeamError(403, "captain_required", "팀장만 대회에 팀을 등록할 수 있습니다.")
    roster = (await db.execute(select(User.id.label("user_id"), User.username).join(
        TeamMember, TeamMember.user_id == User.id,
    ).where(TeamMember.team_id == team_id).order_by(User.id))).all()
    if not roster or actor_id not in {row.user_id for row in roster}:
        raise CompetitionTeamError(409, "invalid_team_roster", "팀장을 포함한 팀 명단이 필요합니다.")
    overlaps = (await db.execute(select(CompetitionTeamMember).where(
        CompetitionTeamMember.competition_id == competition_id,
        CompetitionTeamMember.user_id.in_([row.user_id for row in roster]),
    ))).scalars().all()
    if overlaps:
        raise CompetitionTeamError(409, "roster_overlap", "이미 이 대회의 다른 팀에 등록된 구성원이 있습니다.")

    registration = CompetitionTeamRegistration(
        competition_id=competition_id, team_id=team_id, team_name_snapshot=team.name,
        registered_by=actor_id, registered_at=datetime.now(timezone.utc),
    )
    db.add(registration)
    try:
        # 관계 객체에 의존하지 않고 부모 FK를 먼저 저장하되 commit은 하지 않는다.
        await db.flush()
        db.add_all([CompetitionTeamMember(
            competition_id=competition_id, team_id=team_id,
            user_id=row.user_id, username_snapshot=row.username,
        ) for row in roster])
        await db.flush()
    except IntegrityError as exc:
        raise CompetitionTeamError(409, "registration_conflict", "팀 등록이 충돌했습니다. 다시 확인해 주세요.") from exc
    return registration


async def resolve_team_context(
    db: AsyncSession, competition_id: UUID, user_id: UUID, *, lock: bool = False,
) -> TeamContext | None:
    await _competition(db, competition_id, lock=lock)
    member = (await db.execute(select(CompetitionTeamMember).where(
        CompetitionTeamMember.competition_id == competition_id,
        CompetitionTeamMember.user_id == user_id,
    ))).scalar_one_or_none()
    if member is None:
        return None
    query = select(CompetitionTeamRegistration).where(
        CompetitionTeamRegistration.competition_id == competition_id,
        CompetitionTeamRegistration.team_id == member.team_id,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    registration = (await db.execute(query)).scalar_one()
    members = (await db.execute(select(CompetitionTeamMember).where(
        CompetitionTeamMember.competition_id == competition_id,
        CompetitionTeamMember.team_id == member.team_id,
    ).order_by(CompetitionTeamMember.user_id))).scalars().all()
    return TeamContext(registration, member, tuple(members))


async def get_or_create_team_assignment(
    db: AsyncSession, competition_id: UUID, user_id: UUID, category: str,
    eligible_challenge_ids: list[int],
) -> TeamAssignmentResult:
    context = await resolve_team_context(db, competition_id, user_id, lock=True)
    if context is None:
        raise CompetitionTeamError(403, "team_registration_required", "대회에 등록된 팀 구성원만 선택할 수 있습니다.")
    existing = (await db.execute(select(CompetitionTeamAssignment).where(
        CompetitionTeamAssignment.competition_id == competition_id,
        CompetitionTeamAssignment.team_id == context.team_id,
        CompetitionTeamAssignment.category == category,
    ))).scalar_one_or_none()
    if existing is not None:
        return TeamAssignmentResult(existing, True)

    # Learn의 현재 공개·준비 상태는 호출자가 확인하고, 대회·분류 소속은 여기서 재검사한다.
    pool = (await db.execute(select(CompetitionChallenge.challenge_id).where(
        CompetitionChallenge.competition_id == competition_id,
        CompetitionChallenge.category == category,
        CompetitionChallenge.challenge_id.in_(eligible_challenge_ids),
    ).order_by(CompetitionChallenge.challenge_id))).scalars().all()
    if not pool:
        raise CompetitionTeamError(404, "empty_category", "이 분류에서 배정 가능한 문제가 없습니다.")
    assignment = CompetitionTeamAssignment(
        competition_id=competition_id, team_id=context.team_id, category=category,
        challenge_ids=secrets.SystemRandom().sample(list(pool), min(len(pool), 5)),
        assigned_at=datetime.now(timezone.utc),
    )
    db.add(assignment)
    await db.flush()
    return TeamAssignmentResult(assignment, False)


async def record_team_solve(
    db: AsyncSession, competition_id: UUID, user_id: UUID, challenge_id: int, accepted_at: datetime,
) -> TeamSolveResult:
    context = await resolve_team_context(db, competition_id, user_id, lock=True)
    if context is None:
        raise CompetitionTeamError(403, "team_registration_required", "대회에 등록된 팀 구성원만 풀이를 기록할 수 있습니다.")
    assigned = select(CompetitionTeamAssignment.team_id).where(
        CompetitionTeamAssignment.competition_id == competition_id,
        CompetitionTeamAssignment.team_id == context.team_id,
        CompetitionTeamAssignment.challenge_ids.any(challenge_id),
    ).exists()
    challenge = (await db.execute(select(CompetitionChallenge.challenge_id).where(
        CompetitionChallenge.competition_id == competition_id,
        CompetitionChallenge.challenge_id == challenge_id,
        assigned,
    ))).scalar_one_or_none()
    if challenge is None:
        raise CompetitionTeamError(403, "assigned_challenge_required", "배정된 대회 문제만 풀이를 기록할 수 있습니다.")
    personal = await db.execute(insert(CompetitionSolve).values(
        competition_id=competition_id, user_id=user_id, challenge_id=challenge_id, solved_at=accepted_at,
    ).on_conflict_do_nothing(index_elements=["competition_id", "user_id", "challenge_id"]))
    solve = (await db.execute(select(CompetitionTeamSolve).where(
        CompetitionTeamSolve.competition_id == competition_id,
        CompetitionTeamSolve.team_id == context.team_id,
        CompetitionTeamSolve.challenge_id == challenge_id,
    ))).scalar_one_or_none()
    inserted = solve is None
    earlier = False
    if inserted:
        solve = CompetitionTeamSolve(
            competition_id=competition_id, team_id=context.team_id, challenge_id=challenge_id,
            solved_at=accepted_at, first_solver_id=user_id,
        )
        db.add(solve)
    elif accepted_at < solve.solved_at:
        solve.solved_at = accepted_at
        solve.first_solver_id = user_id
        earlier = True
    await db.flush()
    return TeamSolveResult(context.team_id, personal.rowcount == 1, inserted, earlier, solve)


async def load_team_scoring_rows(
    db: AsyncSession, competition_id: UUID, *, cutoff: datetime | None = None,
) -> TeamScoringRows:
    registrations = (await db.execute(select(CompetitionTeamRegistration).where(
        CompetitionTeamRegistration.competition_id == competition_id,
    ).order_by(CompetitionTeamRegistration.team_id))).scalars().all()
    members = (await db.execute(select(CompetitionTeamMember).where(
        CompetitionTeamMember.competition_id == competition_id,
    ).order_by(CompetitionTeamMember.team_id, CompetitionTeamMember.user_id))).scalars().all()
    query = select(CompetitionTeamSolve).where(CompetitionTeamSolve.competition_id == competition_id)
    if cutoff is not None:
        query = query.where(CompetitionTeamSolve.solved_at < cutoff)
    solves = (await db.execute(query.order_by(
        CompetitionTeamSolve.solved_at, CompetitionTeamSolve.team_id, CompetitionTeamSolve.challenge_id,
    ))).scalars().all()
    return TeamScoringRows(tuple(registrations), tuple(members), tuple(solves))
