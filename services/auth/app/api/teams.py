"""팀 관리 API."""

import hmac
import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.models.team import Team, TeamMember
from app.api.deps import get_current_user, get_optional_user
from app.utils.safe_html import sanitize_rich_content
from app.services import competition_team as team_service

router = APIRouter(tags=["팀"])


# ──── 스키마 ────

class TeamCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    description: str | None = None

    @field_validator("description")
    @classmethod
    def sanitize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_rich_content(value, field_name="팀 소개", allow_empty=True)


class TeamListItem(BaseModel):
    """팀 목록용 응답 — invite_code 미포함."""
    id: UUID
    name: str
    description: str | None
    captain_id: UUID
    total_score: int
    member_count: int = 0

    model_config = {"from_attributes": True}


class TeamResponse(BaseModel):
    """팀 상세용 응답 — invite_code 포함 (소속 멤버만 조회 가능)."""
    id: UUID
    name: str
    description: str | None
    invite_code: str | None = None
    captain_id: UUID
    total_score: int
    member_count: int = 0
    members: list["TeamMemberResponse"] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class TeamMemberResponse(BaseModel):
    user_id: UUID
    username: str
    role: str
    score: int


class TeamJoinRequest(BaseModel):
    invite_code: str = Field(min_length=1, max_length=32)


# ──── 엔드포인트 ────

@router.get("/teams", response_model=list[TeamListItem])
async def list_teams(db: AsyncSession = Depends(get_db)):
    """팀 목록 조회 — invite_code 미포함."""
    from sqlalchemy import func

    result = await db.execute(select(Team).order_by(Team.total_score.desc()))
    teams = result.scalars().all()

    team_ids = [t.id for t in teams]
    if not team_ids:
        return []

    count_result = await db.execute(
        select(TeamMember.team_id, func.count().label("cnt"))
        .where(TeamMember.team_id.in_(team_ids))
        .group_by(TeamMember.team_id)
    )
    counts = {row.team_id: row.cnt for row in count_result}

    return [
        TeamListItem(
            id=t.id,
            name=t.name,
            description=t.description,
            captain_id=t.captain_id,
            total_score=t.total_score,
            member_count=counts.get(t.id, 0),
        )
        for t in teams
    ]


@router.post("/teams", response_model=TeamResponse, status_code=201)
async def create_team(
    body: TeamCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """팀 생성."""
    # 팀명 중복 체크
    existing = await db.execute(select(Team).where(Team.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="이미 사용 중인 팀 이름입니다.")

    team = Team(
        name=body.name,
        description=body.description,
        captain_id=user.id,
    )
    db.add(team)
    await db.flush()

    # 팀장을 멤버로 추가
    db.add(TeamMember(team_id=team.id, user_id=user.id))
    await db.commit()
    await db.refresh(team)

    return TeamResponse(
        id=team.id,
        name=team.name,
        description=team.description,
        invite_code=team.invite_code,
        captain_id=team.captain_id,
        total_score=team.total_score,
        member_count=1,
        members=[TeamMemberResponse(user_id=user.id, username=user.username, role="captain", score=user.xp)],
    )


@router.get("/teams/{team_id}", response_model=TeamResponse)
async def get_team(
    team_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """팀 상세 조회 — invite_code는 소속 멤버에게만 노출."""
    result = await db.execute(select(Team).where(Team.id == team_id))
    team = result.scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="팀을 찾을 수 없습니다.")

    member_rows = (await db.execute(
        select(User.id, User.username, User.xp).join(TeamMember, TeamMember.user_id == User.id)
        .where(TeamMember.team_id == team_id).order_by(TeamMember.joined_at, User.id)
    )).all()
    members = [TeamMemberResponse(user_id=row.id, username=row.username,
        role="captain" if row.id == team.captain_id else "member", score=row.xp) for row in member_rows]

    # 소속 멤버인지 확인하여 invite_code 노출 여부 결정
    show_invite_code = False
    if user:
        member_result = await db.execute(
            select(TeamMember).where(
                TeamMember.team_id == team_id,
                TeamMember.user_id == user.id,
            )
        )
        show_invite_code = member_result.scalar_one_or_none() is not None

    return TeamResponse(
        id=team.id,
        name=team.name,
        description=team.description,
        invite_code=team.invite_code if show_invite_code else None,
        captain_id=team.captain_id,
        total_score=team.total_score,
        member_count=len(members),
        members=members,
    )


@router.post("/teams/{team_id}/join")
async def join_team(
    team_id: UUID,
    invite_code: str | None = None,
    body: TeamJoinRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """초대 코드로 팀 가입."""
    try:
        team = await team_service.lock_team_for_membership(db, team_id)
    except team_service.CompetitionTeamError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error
    supplied_code = body.invite_code if body is not None else invite_code
    if not supplied_code or not hmac.compare_digest(team.invite_code.encode(), supplied_code.encode()):
        raise HTTPException(status_code=400, detail="초대 코드가 올바르지 않습니다.")

    # 이미 멤버인지 확인
    existing = await db.execute(
        select(TeamMember).where(
            TeamMember.team_id == team_id,
            TeamMember.user_id == user.id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="이미 팀에 소속되어 있습니다.")

    db.add(TeamMember(team_id=team_id, user_id=user.id))
    await db.commit()
    return {"message": "팀에 가입했습니다."}


@router.post("/teams/{team_id}/invite")
async def regenerate_invite(
    team_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """초대 코드 재생성 (팀장만)."""
    result = await db.execute(select(Team).where(Team.id == team_id))
    team = result.scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="팀을 찾을 수 없습니다.")
    if team.captain_id != user.id:
        raise HTTPException(status_code=403, detail="팀장만 초대 코드를 변경할 수 있습니다.")

    team.invite_code = secrets.token_urlsafe(16)
    await db.commit()
    return {"invite_code": team.invite_code}


@router.delete("/teams/{team_id}/members/{uid}")
async def remove_member(
    team_id: UUID,
    uid: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """멤버 제거 (팀장만)."""
    try:
        team = await team_service.lock_team_for_membership(db, team_id)
    except team_service.CompetitionTeamError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error
    if team.captain_id != user.id:
        raise HTTPException(status_code=403, detail="팀장만 멤버를 제거할 수 있습니다.")
    if uid == user.id:
        raise HTTPException(status_code=400, detail="팀장은 자신을 제거할 수 없습니다.")

    member_result = await db.execute(
        select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == uid)
    )
    member = member_result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="해당 멤버를 찾을 수 없습니다.")

    await db.delete(member)
    await db.commit()
    return {"message": "멤버가 제거되었습니다."}
