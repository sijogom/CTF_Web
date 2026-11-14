"""대회 문제 접근 권한의 단일 서버 측 판정.

대회 상세 화면, Learn 제출, 환경 생성, 점수 기록이 서로 다른 조건을 사용하면
배정 정책을 우회할 수 있다. 이 모듈은 Auth DB를 기준으로 그 조건을 한 번만 정의한다.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import (
    Competition,
    CompetitionAssignment,
    CompetitionChallenge,
    CompetitionParticipant,
    CompetitionTeamAssignment,
)
from app.services import competition_team as team_service


async def require_competition_challenge_access(
    *,
    competition_id: str,
    user_id: str,
    challenge_id: int,
    db: AsyncSession,
    accepted_at: datetime | None = None,
) -> Competition:
    """대회 공개·기간·참가·문제 포함·개인 배정을 모두 확인한다.

    호출 서비스가 전달한 사용자 ID만 신뢰하지 않고 Auth DB에서 재검증한다. 대회 종료
    뒤에는 새로운 문제 열람·제출·환경 생성을 허용하지 않는다. 단, Learn이 이미 저장한
    정답을 내부 재처리할 때만 accepted_at 기준으로 기간을 판정해 마감 직전의 정상 제출이
    Auth 일시 장애 때문에 점수판에서 사라지지 않게 한다.
    """
    try:
        competition_uuid = UUID(competition_id)
        user_uuid = UUID(user_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="대회 접근 대상을 찾을 수 없습니다.")

    comp_result = await db.execute(
        select(Competition).where(Competition.id == competition_uuid)
    )
    competition = comp_result.scalar_one_or_none()
    if competition is not None and accepted_at is not None and team_service.is_fixed_roster_team(competition):
        competition = (await db.execute(
            select(Competition).where(Competition.id == competition_uuid)
            .with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
    if not competition or competition.is_deleted or not competition.is_published:
        raise HTTPException(status_code=404, detail="대회를 찾을 수 없습니다.")

    now = datetime.now(timezone.utc)
    access_time = now
    if accepted_at is not None:
        if accepted_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="풀이 승인 시각에는 시간대 정보가 필요합니다.")
        access_time = accepted_at.astimezone(timezone.utc)
        # 내부 서비스 시계 오차만 제한적으로 허용한다. 이후 시각을 임의로 보내 대회가
        # 끝난 뒤 풀이를 소급 등록하는 경로는 차단한다.
        if access_time > now + timedelta(seconds=60):
            raise HTTPException(status_code=422, detail="풀이 승인 시각이 현재 시각보다 앞서 있습니다.")

    if access_time < competition.start_at or access_time >= competition.end_at:
        raise HTTPException(status_code=403, detail="현재 접근할 수 없는 대회입니다.")

    team_context = None
    if team_service.is_fixed_roster_team(competition):
        try:
            team_context = await team_service.resolve_team_context(db, competition_uuid, user_uuid)
        except team_service.CompetitionTeamError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error
        if team_context is None:
            raise HTTPException(status_code=403, detail="등록된 팀 명단의 참가자만 접근할 수 있습니다.")
    else:
        participant_result = await db.execute(
            select(CompetitionParticipant).where(
                CompetitionParticipant.competition_id == competition_uuid,
                CompetitionParticipant.user_id == user_uuid,
            )
        )
        if not participant_result.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="대회 참가자만 접근할 수 있습니다.")

    competition_challenge_result = await db.execute(
        select(CompetitionChallenge).where(
            CompetitionChallenge.competition_id == competition_uuid,
            CompetitionChallenge.challenge_id == challenge_id,
        )
    )
    if not competition_challenge_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="대회 문제를 찾을 수 없습니다.")

    if team_context is not None:
        assignment_query = select(CompetitionTeamAssignment.challenge_ids).where(
            CompetitionTeamAssignment.competition_id == competition_uuid,
            CompetitionTeamAssignment.team_id == team_context.team_id,
        )
    else:
        assignment_query = select(CompetitionAssignment.challenge_ids).where(
            CompetitionAssignment.competition_id == competition_uuid,
            CompetitionAssignment.user_id == user_uuid,
        )
    assignment_result = await db.execute(assignment_query)
    assigned = any(
        challenge_id in (challenge_ids or [])
        for challenge_ids in assignment_result.scalars().all()
    )
    if not assigned:
        raise HTTPException(status_code=403, detail="배정된 대회 문제만 접근할 수 있습니다.")

    return competition
