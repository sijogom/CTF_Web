"""CTF 대회 API."""

import logging
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, delete, select, func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.competition import (
    Competition,
    CompetitionChallenge,
    CompetitionAssignment,
    CompetitionDeletionConfirmation,
    CompetitionParticipant,
    CompetitionSolve,
    CompetitionTeamRegistration,
    CompetitionTeamMember,
    CompetitionTeamAssignment,
    CompetitionTeamSolve,
)
from app.services import competition_team as team_service
from app.utils.scoring import (
    calc_dynamic_value,
    competition_freeze_window,
    competition_challenge_values,
    competition_scoreboard_entries,
    competition_scoreboard_series,
)
from app.api.deps import get_current_user, get_optional_user, require_role
from app.utils.audit import record_audit
from app.utils.safe_html import (
    RichContentError,
    normalize_rich_content_for_write,
    sanitize_rich_content,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["대회"])

_DELETION_CONFIRMATION_TTL = timedelta(minutes=10)
_ENVIRONMENT_STATUSES = frozenset(
    {"not_required", "ready", "legacy_unverified", "verification_required"}
)


async def _competition_deletion_impact(db: AsyncSession, comp_id: UUID) -> dict[str, int]:
    """보관 삭제 전에 관리자에게 표시할 관계 데이터 수를 계산한다."""
    counts: dict[str, int] = {}
    for name, model in (
        ("challenge_mappings", CompetitionChallenge),
        ("assignments", CompetitionAssignment),
        ("participants", CompetitionParticipant),
        ("solves", CompetitionSolve),
        ("registered_teams", CompetitionTeamRegistration),
        ("team_members", CompetitionTeamMember),
        ("team_assignments", CompetitionTeamAssignment),
        ("team_solves", CompetitionTeamSolve),
    ):
        result = await db.execute(
            select(func.count()).select_from(model).where(model.competition_id == comp_id)
        )
        counts[name] = result.scalar() or 0
    return counts


def _team_registration_payload(registration, members) -> dict:
    return {
        "team_id": str(registration.team_id), "team_name": registration.team_name_snapshot,
        "registered_at": registration.registered_at.isoformat(),
        "members": [{"user_id": str(member.user_id), "username": member.username_snapshot}
                    for member in members],
    }


async def _team_call(operation):
    try:
        return await operation
    except team_service.CompetitionTeamError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error


async def _participant_count(db, comp) -> int:
    model = CompetitionTeamRegistration if team_service.is_fixed_roster_team(comp) else CompetitionParticipant
    return (await db.execute(select(func.count()).select_from(model).where(model.competition_id == comp.id))).scalar() or 0


async def _team_detail_context(db, comp, user):
    if not comp.is_deleted:
        return await _team_call(team_service.resolve_team_context(db, comp.id, user.id))
    # 관리자 보관 미리보기만 허용하며 실제 문제 접근은 access가 계속 거절한다.
    if user.role not in ("admin", "moderator"):
        raise HTTPException(status_code=404, detail="대회를 찾을 수 없습니다.")
    rows = await _team_call(team_service.load_team_scoring_rows(db, comp.id))
    member = next((row for row in rows.members if row.user_id == user.id), None)
    if member is None:
        return None
    registration = next((row for row in rows.registrations if row.team_id == member.team_id), None)
    if registration is None:
        return None
    members = tuple(row for row in rows.members if row.team_id == member.team_id)
    return team_service.TeamContext(registration, member, members)


async def _require_mode_change_allowed(db, comp, requested_mode, now):
    if requested_mode == comp.mode:
        return
    if now >= comp.start_at or team_service.is_fixed_roster_team(comp):
        raise HTTPException(status_code=409, detail="시작되었거나 고정 명단을 사용하는 대회의 모드는 변경할 수 없습니다.")
    for model in (CompetitionParticipant, CompetitionAssignment, CompetitionSolve):
        count = (await db.execute(select(func.count()).select_from(model).where(model.competition_id == comp.id))).scalar()
        if count:
            raise HTTPException(status_code=409, detail="참가·배정·풀이 기록이 있는 대회의 모드는 변경할 수 없습니다.")


async def _team_score_response(db, comp, *, graph=False):
    is_frozen, cutoff = competition_freeze_window(comp.end_at, comp.freeze_minutes, datetime.now(timezone.utc))
    rows = await _team_call(team_service.load_team_scoring_rows(db, comp.id, cutoff=cutoff))
    configs = (await db.execute(select(CompetitionChallenge).where(CompetitionChallenge.competition_id == comp.id))).scalars().all()
    solve_counts = {}
    solves = []
    for row in rows.solves:
        solve_counts[row.challenge_id] = solve_counts.get(row.challenge_id, 0) + 1
        solves.append((str(row.team_id), row.challenge_id, row.solved_at))
    values = competition_challenge_values({row.challenge_id: row for row in configs}, solve_counts)
    names = {str(row.team_id): row.team_name_snapshot for row in rows.registrations}
    if graph:
        series = competition_scoreboard_series(comp.start_at, sorted(solves, key=lambda row: row[2]), values, names)
        return {"team_contract": comp.team_contract, "series": [
            {"team_id": row["subject_id"], "team_name": row["name"], "data": row["data"]} for row in series
        ]}
    entries = competition_scoreboard_entries(list(names.items()), solves, values)
    return {"team_contract": comp.team_contract, "is_frozen": is_frozen, "entries": [
        {"rank": row["rank"], "team_id": row["subject_id"], "team_name": row["name"],
         "score": row["score"], "solves": row["solves"], "first_bloods": row["first_bloods"]}
        for row in entries
    ]}


def _require_not_deleted(comp: Competition) -> None:
    if comp.is_deleted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="삭제된 대회는 변경할 수 없습니다.")


async def _require_active_competition(db: AsyncSession, comp_id: UUID) -> Competition:
    result = await db.execute(select(Competition).where(Competition.id == comp_id))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="대회를 찾을 수 없습니다.")
    _require_not_deleted(comp)
    return comp


async def _fetch_challenge_names(challenge_ids: list[int]) -> dict[int, str]:
    """learn-service에서 챌린지 ID → 이름 매핑을 일괄 조회."""
    if not challenge_ids:
        return {}
    headers: dict[str, str] = {}
    if settings.internal_api_key:
        headers["X-Internal-Key"] = settings.internal_api_key
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{settings.learn_service_url}/internal/challenges/batch-names",
                json={"challenge_ids": challenge_ids},
                headers=headers,
            )
            if resp.status_code == 200:
                return {int(k): v for k, v in resp.json().items()}
    except Exception as exc:
        logger.warning("learn-service 챌린지 이름 조회 실패: %s", exc)
    return {}


async def _fetch_challenge_info(challenge_ids: list[int]) -> dict[int, dict]:
    """learn-service에서 대회 매핑 검증용 챌린지 메타데이터를 일괄 조회."""
    if not challenge_ids:
        return {}
    headers: dict[str, str] = {}
    if settings.internal_api_key:
        headers["X-Internal-Key"] = settings.internal_api_key
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{settings.learn_service_url}/internal/challenges/batch-info",
                json={"challenge_ids": challenge_ids},
                headers=headers,
            )
            if resp.status_code == 200:
                return {int(k): v for k, v in resp.json().items()}
    except Exception as exc:
        logger.warning("learn-service 챌린지 정보 조회 실패: %s", exc)
    return {}


def _is_publishable_competition_challenge(info: dict | None) -> bool:
    """대회에 배정 가능한 공개 CTF 문제인지 확인한다.

    문제의 원본 공개 상태와 유형은 Learn 서비스가 소유한다. Auth DB의 단순 ID 매핑만으로
    워게임이나 비공개 문제를 CTF 화면에 노출하지 않도록 이 조건을 한 곳에서 사용한다.
    """
    return bool(
        info
        and info.get("is_published") is True
        and info.get("challenge_type") == "competition"
        and (
            info.get("env_type") in (None, "none")
            or info.get("environment_ready") is True
        )
    )


def _safe_challenge_environment_status(info: dict) -> str:
    """Pass through only the small, UI-safe readiness status vocabulary."""
    current_status = info.get("environment_status")
    if current_status in _ENVIRONMENT_STATUSES:
        return current_status
    if info.get("env_type") in (None, "none"):
        return "not_required"
    return "verification_required"


async def _require_publishable_competition_challenge(challenge_id: int) -> dict:
    """관리자 대회 매핑 전에 Learn의 공개 CTF 문제 여부를 fail-closed로 검증한다."""
    info = (await _fetch_challenge_info([challenge_id])).get(challenge_id)
    if info is None:
        raise HTTPException(
            status_code=502,
            detail="챌린지 정보를 확인하지 못해 대회에 추가할 수 없습니다.",
        )
    if not _is_publishable_competition_challenge(info):
        raise HTTPException(
            status_code=422,
            detail="공개된 CTF 대회 문제만 대회에 추가할 수 있습니다.",
        )
    return info


# ──── 스키마 ────

class CompetitionCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    mode: str = Field("individual", pattern=r"^(individual|team)$")
    start_at: datetime
    end_at: datetime
    freeze_minutes: int = Field(30, ge=0)
    banner_url: str | None = None

    @field_validator("description")
    @classmethod
    def sanitize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_rich_content(value, field_name="대회 설명", allow_empty=True)


class CompetitionUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    mode: str | None = Field(None, pattern=r"^(individual|team)$")
    start_at: datetime | None = None
    end_at: datetime | None = None
    freeze_minutes: int | None = Field(None, ge=0)
    banner_url: str | None = None

    @field_validator("description")
    @classmethod
    def sanitize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_rich_content(value, field_name="대회 설명", allow_empty=True)


class CompetitionResponse(BaseModel):
    id: UUID
    title: str
    description: str | None
    mode: str
    team_contract: Literal["fixed_roster_v1"] | None = None
    start_at: datetime
    end_at: datetime
    freeze_minutes: int
    banner_url: str | None = None
    is_published: bool
    is_deleted: bool = False
    deleted_at: datetime | None = None
    participant_count: int = 0
    status: str = "upcoming"  # upcoming, ongoing, ended

    model_config = {"from_attributes": True}


class CompetitionDeletionImpactResponse(BaseModel):
    resource_type: str = "competition"
    target_id: UUID
    target_name: str
    is_deleted: bool
    related_counts: dict[str, int]
    recovery_available: bool
    deleted_at: datetime | None = None


class CompetitionDeletionConfirmationRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)
    target_name: str = Field(..., min_length=1, max_length=200)


class CompetitionDeletionConfirmationResponse(BaseModel):
    confirmation_token: str
    expires_at: datetime


class CompetitionDeletionExecuteRequest(BaseModel):
    confirmation_token: str = Field(..., min_length=20, max_length=256)


class CompetitionDeletionActionResponse(BaseModel):
    message: str
    target_id: UUID
    is_deleted: bool
    recovery_available: bool


class ScoreboardEntry(BaseModel):
    rank: int
    user_id: UUID
    username: str
    score: int


class CompetitionJoinRequest(BaseModel):
    team_id: UUID


# ──── 엔드포인트 ────

@router.get("/competitions", response_model=list[CompetitionResponse])
async def list_competitions(
    status_filter: str | None = Query(None, alias="status"),
    include_all: bool = Query(False, alias="all"),
    include_deleted: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """대회 목록 조회. 관리자는 all=true와 include_deleted=true를 별도로 요청할 수 있다."""
    query = select(Competition)
    is_admin = bool(user and user.role in ("admin", "moderator"))
    if not (include_deleted and is_admin):
        query = query.where(Competition.is_deleted.is_(False))
    # 관리자가 all=true 요청 시 미공개 포함, 그 외에는 공개 대회만
    if not (include_all and is_admin):
        query = query.where(Competition.is_published.is_(True))

    now = datetime.now(timezone.utc)
    if status_filter == "upcoming":
        query = query.where(Competition.start_at > now)
    elif status_filter == "ongoing":
        query = query.where(Competition.start_at <= now, Competition.end_at > now)
    elif status_filter == "ended":
        query = query.where(Competition.end_at <= now)

    query = query.order_by(Competition.start_at.desc())
    result = await db.execute(query)
    competitions = result.scalars().all()

    responses = []
    for comp in competitions:
        # 참가자 수 조회
        count = await _participant_count(db, comp)

        # 상태 판별
        if now < comp.start_at:
            comp_status = "upcoming"
        elif now >= comp.end_at:
            comp_status = "ended"
        else:
            comp_status = "ongoing"

        responses.append(CompetitionResponse(
            id=comp.id,
            title=comp.title,
            description=comp.description,
            mode=comp.mode,
            team_contract=comp.team_contract,
            start_at=comp.start_at,
            end_at=comp.end_at,
            freeze_minutes=comp.freeze_minutes,
            banner_url=comp.banner_url,
            is_published=comp.is_published,
            is_deleted=comp.is_deleted,
            deleted_at=comp.deleted_at,
            participant_count=count,
            status=comp_status,
        ))

    return responses


@router.post("/competitions", response_model=CompetitionResponse, status_code=201)
async def create_competition(
    body: CompetitionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "moderator")),
):
    """대회 생성 (admin/moderator 전용)."""
    if body.end_at <= body.start_at:
        raise HTTPException(status_code=400, detail="종료 시간은 시작 시간 이후여야 합니다.")

    comp = Competition(
        title=body.title,
        description=body.description,
        mode=body.mode,
        team_contract="fixed_roster_v1" if body.mode == "team" else None,
        start_at=body.start_at,
        end_at=body.end_at,
        freeze_minutes=body.freeze_minutes,
        banner_url=body.banner_url,
        created_by=user.id,
    )
    db.add(comp)
    await db.flush()
    from app.models.personal_notification import CompetitionNotificationState
    db.add(CompetitionNotificationState(competition_id=comp.id))
    await db.commit()
    await db.refresh(comp)

    return CompetitionResponse(
        id=comp.id,
        title=comp.title,
        description=comp.description,
        mode=comp.mode,
        team_contract=comp.team_contract,
        start_at=comp.start_at,
        end_at=comp.end_at,
        freeze_minutes=comp.freeze_minutes,
        banner_url=comp.banner_url,
        is_published=comp.is_published,
        is_deleted=comp.is_deleted,
        deleted_at=comp.deleted_at,
        participant_count=0,
        status="upcoming",
    )


@router.patch("/competitions/{comp_id}/publish")
async def toggle_publish(
    comp_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "moderator")),
):
    """대회 공개/비공개 토글 (admin/moderator 전용)."""
    result = await db.execute(select(Competition).where(Competition.id == comp_id).with_for_update())
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(status_code=404, detail="대회를 찾을 수 없습니다.")
    _require_not_deleted(comp)

    comp.is_published = not comp.is_published
    from app.utils.personal_notifications import record_competition_publish
    await record_competition_publish(db, comp)
    await db.commit()
    await db.refresh(comp)

    return {"is_published": comp.is_published}


@router.patch("/competitions/{comp_id}", response_model=CompetitionResponse)
async def update_competition(
    comp_id: UUID,
    body: CompetitionUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "moderator")),
):
    """대회 수정 (admin/moderator 전용)."""
    result = await db.execute(select(Competition).where(Competition.id == comp_id)
                              .with_for_update().execution_options(populate_existing=True))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(status_code=404, detail="대회를 찾을 수 없습니다.")
    _require_not_deleted(comp)

    update_data = body.model_dump(exclude_unset=True)
    if "mode" in update_data:
        if update_data["mode"] is None:
            raise HTTPException(status_code=422, detail="대회 모드를 비울 수 없습니다.")
        await _require_mode_change_allowed(db, comp, update_data["mode"], datetime.now(timezone.utc))
    if "description" not in update_data and comp.description is not None:
        try:
            update_data["description"] = normalize_rich_content_for_write(
                comp.description,
                field_name="대회 설명",
                allow_empty=True,
            )
        except RichContentError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=error.as_detail(),
            ) from error
    for field, value in update_data.items():
        setattr(comp, field, value)

    if comp.end_at <= comp.start_at:
        raise HTTPException(status_code=400, detail="종료 시간은 시작 시간 이후여야 합니다.")

    await db.commit()
    await db.refresh(comp)

    now = datetime.now(timezone.utc)
    if now < comp.start_at:
        comp_status = "upcoming"
    elif now >= comp.end_at:
        comp_status = "ended"
    else:
        comp_status = "ongoing"

    count = await _participant_count(db, comp)

    return CompetitionResponse(
        id=comp.id,
        title=comp.title,
        description=comp.description,
        mode=comp.mode,
        team_contract=comp.team_contract,
        start_at=comp.start_at,
        end_at=comp.end_at,
        freeze_minutes=comp.freeze_minutes,
        banner_url=comp.banner_url,
        is_published=comp.is_published,
        is_deleted=comp.is_deleted,
        deleted_at=comp.deleted_at,
        participant_count=count,
        status=comp_status,
    )


@router.post("/competitions/upload-banner")
async def upload_banner(
    file: UploadFile = File(...),
    user: User = Depends(require_role("admin", "moderator")),
):
    """대회 배너 이미지 업로드."""
    from app.utils.minio_client import (
        upload_banner as do_upload,
        ALLOWED_IMAGE_TYPES,
        MAX_BANNER_SIZE,
    )

    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="이미지 파일만 업로드 가능합니다 (JPEG, PNG, WebP, GIF).")

    data = await file.read()
    if len(data) > MAX_BANNER_SIZE:
        raise HTTPException(status_code=400, detail="파일 크기가 5MB를 초과합니다.")

    url = do_upload(data, file.filename or "banner.png", file.content_type)
    return {"banner_url": url}


@router.get("/competitions/{comp_id}")
async def get_competition(
    comp_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """대회 상세 조회."""
    result = await db.execute(select(Competition).where(Competition.id == comp_id))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(status_code=404, detail="대회를 찾을 수 없습니다.")

    # 비공개 대회는 관리자/모더레이터만 조회 가능
    is_admin = bool(user and user.role in ("admin", "moderator"))
    if comp.is_deleted and not is_admin:
        raise HTTPException(status_code=404, detail="대회를 찾을 수 없습니다.")
    if not comp.is_published and not is_admin:
        raise HTTPException(status_code=404, detail="대회를 찾을 수 없습니다.")

    now = datetime.now(timezone.utc)
    if now < comp.start_at:
        comp_status = "upcoming"
    elif now >= comp.end_at:
        comp_status = "ended"
    else:
        comp_status = "ongoing"

    # 참가자 수
    fixed_team = team_service.is_fixed_roster_team(comp)
    participant_count = await _participant_count(db, comp)

    # 현재 유저 참가 여부 + 배정 문제
    joined = False
    assignment_rows: list[CompetitionAssignment] = []
    my_challenges: list[dict] = []
    team_context = None
    if user:
        if fixed_team:
            team_context = await _team_detail_context(db, comp, user)
            joined = team_context is not None
        else:
            participant_result = await db.execute(
                select(CompetitionParticipant).where(
                    CompetitionParticipant.competition_id == comp_id,
                    CompetitionParticipant.user_id == user.id,
                )
            )
            joined = participant_result.scalar_one_or_none() is not None

        if joined:
            assignment_model = CompetitionTeamAssignment if fixed_team else CompetitionAssignment
            subject_filter = (assignment_model.team_id == team_context.team_id if fixed_team
                              else assignment_model.user_id == user.id)
            assignment_result = await db.execute(
                select(assignment_model).where(
                    assignment_model.competition_id == comp_id,
                    subject_filter,
                )
            )
            assignment_rows = assignment_result.scalars().all()
            my_challenges = [
                {
                    "category": a.category,
                    "challenge_ids": a.challenge_ids,
                }
                for a in assignment_rows
            ]

    # 대회에 등록된 챌린지 메타데이터
    challenge_result = await db.execute(
        select(CompetitionChallenge).where(
            CompetitionChallenge.competition_id == comp_id
        )
    )
    all_challenges = challenge_result.scalars().all()

    # 기존 DB에 과거 설정 실수로 남아 있을 수 있는 비공개·워게임 매핑도 참가자에게
    # 노출하지 않는다. Learn 메타데이터를 확인할 수 없으면 문제 목록을 fail-closed로
    # 비워 직접 설명·파일·환경 접근과 UI 표시가 불일치하지 않게 한다.
    all_challenge_info_map = await _fetch_challenge_info(
        [challenge.challenge_id for challenge in all_challenges]
    )
    valid_challenge_ids = {
        challenge_id
        for challenge_id, info in all_challenge_info_map.items()
        if _is_publishable_competition_challenge(info)
    }
    invalid_challenge_ids = {
        challenge.challenge_id
        for challenge in all_challenges
        if challenge.challenge_id not in valid_challenge_ids
    }
    if invalid_challenge_ids:
        logger.warning(
            "대회 매핑에서 공개 CTF 조건을 충족하지 않는 문제를 제외합니다: competition=%s count=%d",
            comp_id,
            len(invalid_challenge_ids),
        )
    all_challenges = [
        challenge
        for challenge in all_challenges
        if challenge.challenge_id in valid_challenge_ids
    ]

    assigned_challenge_ids = {
        challenge_id
        for assignment in assignment_rows
        for challenge_id in (assignment.challenge_ids or [])
    }
    assigned_categories = {assignment.category for assignment in assignment_rows}

    category_counts: dict[str, int] = {}
    for challenge in all_challenges:
        category_counts[challenge.category] = category_counts.get(challenge.category, 0) + 1

    # 일반 참가자는 자신에게 배정된 문제만 볼 수 있다. 관리자는 기존의 대회 관리
    # 미리보기 호환성을 위해 전체 문제 메타데이터를 계속 받을 수 있지만, 점수 기록과
    # 환경 생성 같은 내부 권한 API는 동일하게 참가·배정 조건을 강제한다.
    if is_admin:
        visible_challenge_rows = all_challenges
    elif joined:
        visible_challenge_rows = [
            challenge
            for challenge in all_challenges
            if challenge.challenge_id in assigned_challenge_ids
        ]
    else:
        visible_challenge_rows = []

    if not joined:
        assignment_state = "not_joined"
    elif assignment_rows:
        assignment_state = "assigned"
    elif comp_status == "ongoing":
        assignment_state = "select_category"
    else:
        assignment_state = "not_joined"

    # 챌린지별 풀이 수 집계
    solve_model = CompetitionTeamSolve if fixed_team else CompetitionSolve
    solve_counts_result = await db.execute(
        select(
            solve_model.challenge_id,
            func.count().label("cnt"),
        )
        .where(solve_model.competition_id == comp_id)
        .group_by(solve_model.challenge_id)
    )
    solve_counts = {r.challenge_id: r.cnt for r in solve_counts_result.all()}

    # 사용자 풀이 여부
    user_solved_ids: set[int] = set()
    if user:
        user_solves_result = await db.execute(
            select(CompetitionSolve.challenge_id).where(
                CompetitionSolve.competition_id == comp_id,
                CompetitionSolve.user_id == user.id,
            )
        )
        user_solved_ids = {r.challenge_id for r in user_solves_result.all()}

    # First Blood: 챌린지별 최초 풀이자

    fb_subq = (
        select(
            solve_model.challenge_id,
            func.min(solve_model.solved_at).label("first_at"),
        )
        .where(solve_model.competition_id == comp_id)
        .group_by(solve_model.challenge_id)
        .subquery()
    )
    name_column = CompetitionTeamRegistration.team_name_snapshot if fixed_team else User.username
    fb_query = (
        select(solve_model.challenge_id, name_column.label("first_blood_name"))
        .join(fb_subq, and_(
            solve_model.challenge_id == fb_subq.c.challenge_id,
            solve_model.solved_at == fb_subq.c.first_at,
        ))
        .where(solve_model.competition_id == comp_id)
    )
    if fixed_team:
        fb_query = fb_query.join(CompetitionTeamRegistration, and_(
            CompetitionTeamRegistration.competition_id == CompetitionTeamSolve.competition_id,
            CompetitionTeamRegistration.team_id == CompetitionTeamSolve.team_id,
        ))
    else:
        fb_query = fb_query.join(User, User.id == CompetitionSolve.user_id)
    fb_result = await db.execute(fb_query)
    first_blood_map = {row.challenge_id: row.first_blood_name for row in fb_result.all()}

    team_solved_ids = set()
    if fixed_team and team_context is not None:
        team_solved_ids = set((await db.execute(
            select(CompetitionTeamSolve.challenge_id).where(
                CompetitionTeamSolve.competition_id == comp_id,
                CompetitionTeamSolve.team_id == team_context.team_id,
            )
        )).scalars().all())

    # learn-service에서 챌린지 정보(이름+설명)를 일괄 조회
    visible_challenge_ids = [c.challenge_id for c in visible_challenge_rows]
    challenge_info_map = {
        challenge_id: all_challenge_info_map[challenge_id]
        for challenge_id in visible_challenge_ids
        if challenge_id in all_challenge_info_map
    }

    challenges = []
    for c in visible_challenge_rows:
        solves = solve_counts.get(c.challenge_id, 0)
        if c.scoring_type == "dynamic":
            value = calc_dynamic_value(c.initial_value, c.decay, c.minimum_value, solves)
        else:
            value = c.initial_value
        info = challenge_info_map.get(c.challenge_id, {})
        challenges.append({
            "id": c.challenge_id,
            "name": info.get("name", f"{c.category}-{c.challenge_id}"),
            "category": c.category,
            "difficulty": c.difficulty,
            "value": value,
            "solves": solves,
            "scoring_type": c.scoring_type,
            "solved": c.challenge_id in user_solved_ids,
            "first_blood": first_blood_map.get(c.challenge_id),
            "description": info.get("description", ""),
            "env_type": info.get("env_type", "none"),
            "container_port": info.get("container_port"),
            "environment_ready": info.get("environment_ready") is True,
            "environment_status": _safe_challenge_environment_status(info),
            **({"team_solved": c.challenge_id in team_solved_ids} if fixed_team else {}),
        })

    return {
        "id": str(comp.id),
        "title": comp.title,
        "description": comp.description or "",
        "mode": comp.mode,
        "team_contract": comp.team_contract,
        "team_registration": (_team_registration_payload(team_context.registration, team_context.members)
                              if team_context is not None else None),
        "start_at": comp.start_at.isoformat(),
        "end_at": comp.end_at.isoformat(),
        "freeze_minutes": comp.freeze_minutes,
        "banner_url": comp.banner_url,
        "is_published": comp.is_published,
        "is_deleted": comp.is_deleted,
        "deleted_at": comp.deleted_at.isoformat() if comp.deleted_at else None,
        "participant_count": participant_count,
        "status": comp_status,
        "joined": joined,
        "my_challenges": my_challenges if comp_status == "ongoing" else [],
        "assignment": {
            "state": assignment_state,
            "categories": [
                {
                    "category": category,
                    "assigned": category in assigned_categories,
                    "available_count": count,
                }
                for category, count in sorted(category_counts.items())
            ],
        },
        "visible_challenges": challenges if comp_status == "ongoing" else [],
        # 기존 프런트엔드/외부 소비자가 전환할 수 있도록 유지한다. 일반 참가자에게는
        # visible_challenges와 같은 배정된 문제만 반환한다.
        "challenges": challenges if comp_status == "ongoing" else [],
    }


@router.get("/competitions/{comp_id}/teams")
async def registered_competition_teams(comp_id: UUID, db: AsyncSession = Depends(get_db)):
    comp = (await db.execute(select(Competition).where(Competition.id == comp_id))).scalar_one_or_none()
    if comp is None or comp.is_deleted or not comp.is_published:
        raise HTTPException(status_code=404, detail="대회를 찾을 수 없습니다.")
    if not team_service.is_fixed_roster_team(comp):
        return []
    rows = await _team_call(team_service.load_team_scoring_rows(db, comp_id))
    members = {}
    for member in rows.members:
        members.setdefault(member.team_id, []).append(member)
    return [_team_registration_payload(row, members.get(row.team_id, [])) for row in rows.registrations]


@router.post("/competitions/{comp_id}/join")
async def join_competition(
    comp_id: UUID,
    body: CompetitionJoinRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """대회 참가."""
    # 대회 존재 + 공개 확인
    result = await db.execute(select(Competition).where(Competition.id == comp_id)
                              .with_for_update().execution_options(populate_existing=True))
    comp = result.scalar_one_or_none()
    if not comp or comp.is_deleted or not comp.is_published:
        raise HTTPException(status_code=404, detail="대회를 찾을 수 없습니다.")

    now = datetime.now(timezone.utc)
    if now >= comp.end_at:
        raise HTTPException(status_code=400, detail="이미 종료된 대회입니다.")

    if team_service.is_fixed_roster_team(comp):
        if body is None:
            raise HTTPException(status_code=422, detail="등록할 팀을 선택하세요.")
        registration = await _team_call(team_service.register_team(db, comp_id, body.team_id, user.id))
        context = await _team_call(team_service.resolve_team_context(db, comp_id, user.id))
        if context is None or context.team_id != registration.team_id:
            raise HTTPException(status_code=409, detail="고정된 팀 명단을 확인할 수 없습니다.")
        payload = _team_registration_payload(registration, context.members)
        await db.commit()
        return {"message": "대회에 참가했습니다.", "team_registration": payload}
    if body is not None:
        raise HTTPException(status_code=422, detail="이 대회는 개인 참가 방식입니다.")

    # 중복 참가 확인
    existing = await db.execute(
        select(CompetitionParticipant).where(
            CompetitionParticipant.competition_id == comp_id,
            CompetitionParticipant.user_id == user.id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="이미 참가한 대회입니다.")

    participant = CompetitionParticipant(
        competition_id=comp_id,
        user_id=user.id,
    )
    db.add(participant)
    await db.commit()

    return {"message": "대회에 참가했습니다."}


@router.post("/competitions/{comp_id}/select-category")
async def select_category(
    comp_id: UUID,
    category: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """분야 선택 → 랜덤 문제 배정."""
    # 대회 진행 중인지 확인
    comp_result = await db.execute(select(Competition).where(Competition.id == comp_id))
    comp = comp_result.scalar_one_or_none()
    if not comp or comp.is_deleted or not comp.is_published:
        raise HTTPException(status_code=404, detail="대회를 찾을 수 없습니다.")
    fixed_team = team_service.is_fixed_roster_team(comp)
    if fixed_team:
        comp = (await db.execute(select(Competition).where(Competition.id == comp_id)
                                 .with_for_update().execution_options(populate_existing=True))).scalar_one()
        if comp.is_deleted or not comp.is_published:
            raise HTTPException(status_code=404, detail="대회를 찾을 수 없습니다.")
    now = datetime.now(timezone.utc)
    if now < comp.start_at:
        raise HTTPException(status_code=400, detail="아직 시작되지 않은 대회입니다.")
    if now >= comp.end_at:
        raise HTTPException(status_code=400, detail="이미 종료된 대회입니다.")

    # 참가 확인
    team_context = None
    if fixed_team:
        team_context = await _team_call(team_service.resolve_team_context(db, comp_id, user.id))
        if team_context is None:
            raise HTTPException(status_code=400, detail="먼저 팀을 대회에 등록해야 합니다.")
    else:
        participant = await db.execute(
            select(CompetitionParticipant).where(
                CompetitionParticipant.competition_id == comp_id,
                CompetitionParticipant.user_id == user.id,
            )
        )
        if not participant.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="먼저 대회에 참가해야 합니다.")

    # 이미 해당 카테고리 배정 확인
    assignment_model = CompetitionTeamAssignment if fixed_team else CompetitionAssignment
    subject_filter = (assignment_model.team_id == team_context.team_id if fixed_team
                      else assignment_model.user_id == user.id)
    existing = await db.execute(
        select(assignment_model).where(
            assignment_model.competition_id == comp_id,
            subject_filter,
            assignment_model.category == category,
        )
    )
    existing_assignment = existing.scalar_one_or_none()
    if existing_assignment:
        # 버튼 중복 클릭·네트워크 재시도에서도 최초 배정을 재생성하지 않는다.
        return {
            "category": existing_assignment.category,
            "challenge_ids": existing_assignment.challenge_ids,
            "already_assigned": True,
        }

    # 해당 카테고리 문제 풀에서 랜덤 선택
    challenges_result = await db.execute(
        select(CompetitionChallenge).where(
            CompetitionChallenge.competition_id == comp_id,
            CompetitionChallenge.category == category,
        )
    )
    pool = challenges_result.scalars().all()
    pool_info = await _fetch_challenge_info([challenge.challenge_id for challenge in pool])
    pool = [
        challenge
        for challenge in pool
        if _is_publishable_competition_challenge(pool_info.get(challenge.challenge_id))
    ]
    if not pool:
        raise HTTPException(
            status_code=404,
            detail="배정 가능한 공개 CTF 문제가 없습니다.",
        )

    if fixed_team:
        if datetime.now(timezone.utc) >= comp.end_at:
            raise HTTPException(status_code=400, detail="이미 종료된 대회입니다.")
        result = await _team_call(team_service.get_or_create_team_assignment(
            db, comp_id, user.id, category, [challenge.challenge_id for challenge in pool],
        ))
        await db.commit()
        return {"category": result.assignment.category, "challenge_ids": result.assignment.challenge_ids,
                "already_assigned": result.already_assigned}

    # 난이도별 분배 (기본: easy 40%, medium 35%, hard 25%)
    # secrets.SystemRandom: 암호학적으로 안전한 난수 생성기 (대회 공정성 보장)
    secure_rng = secrets.SystemRandom()
    selected_ids = [c.challenge_id for c in secure_rng.sample(pool, min(len(pool), 5))]

    assignment = CompetitionAssignment(
        competition_id=comp_id,
        user_id=user.id,
        category=category,
        challenge_ids=selected_ids,
    )
    db.add(assignment)
    try:
        await db.commit()
    except IntegrityError:
        # 복수 요청이 동시에 도착하면 DB의 복합 PK를 최종 경계로 사용하고, 먼저
        # 성공한 배정 결과를 반환한다.
        await db.rollback()
        existing = await db.execute(
            select(CompetitionAssignment).where(
                CompetitionAssignment.competition_id == comp_id,
                CompetitionAssignment.user_id == user.id,
                CompetitionAssignment.category == category,
            )
        )
        existing_assignment = existing.scalar_one_or_none()
        if existing_assignment:
            return {
                "category": existing_assignment.category,
                "challenge_ids": existing_assignment.challenge_ids,
                "already_assigned": True,
            }
        raise

    return {"category": category, "challenge_ids": selected_ids, "already_assigned": False}


@router.get("/competitions/{comp_id}/my-challenges")
async def my_challenges(
    comp_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """내 배정 문제 조회."""
    comp_result = await db.execute(select(Competition).where(Competition.id == comp_id))
    comp = comp_result.scalar_one_or_none()
    if not comp or comp.is_deleted or not comp.is_published:
        raise HTTPException(status_code=404, detail="대회를 찾을 수 없습니다.")
    fixed_team = team_service.is_fixed_roster_team(comp)
    team_context = None
    if fixed_team:
        team_context = await _team_call(team_service.resolve_team_context(db, comp_id, user.id))
        if team_context is None:
            return []
    assignment_model = CompetitionTeamAssignment if fixed_team else CompetitionAssignment
    subject_filter = (assignment_model.team_id == team_context.team_id if fixed_team
                      else assignment_model.user_id == user.id)
    result = await db.execute(
        select(assignment_model).where(
            assignment_model.competition_id == comp_id,
            subject_filter,
        )
    )
    assignments = result.scalars().all()

    return [
        {
            "category": a.category,
            "challenge_ids": a.challenge_ids,
            "assigned_at": a.assigned_at.isoformat(),
            **({"team_id": str(team_context.team_id)} if fixed_team else {}),
        }
        for a in assignments
    ]


@router.get("/competitions/{comp_id}/scoreboard")
async def scoreboard(
    comp_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """스코어보드 조회 — Dynamic Value 실시간 계산 + 프리즈 지원."""
    # 대회 확인 (공개된 대회만)
    comp_result = await db.execute(select(Competition).where(Competition.id == comp_id))
    comp = comp_result.scalar_one_or_none()
    if not comp or comp.is_deleted or not comp.is_published:
        raise HTTPException(status_code=404, detail="대회를 찾을 수 없습니다.")

    if team_service.is_fixed_roster_team(comp):
        return await _team_score_response(db, comp)
    is_frozen, freeze_start = competition_freeze_window(comp.end_at, comp.freeze_minutes, datetime.now(timezone.utc))

    # freeze 구간이면 freeze_start 이전 풀이만 집계하기 위한 공통 필터
    base_solve_filter = [CompetitionSolve.competition_id == comp_id]
    if is_frozen:
        base_solve_filter.append(CompetitionSolve.solved_at < freeze_start)

    # 1) 대회 챌린지 설정 조회
    cc_result = await db.execute(
        select(CompetitionChallenge).where(
            CompetitionChallenge.competition_id == comp_id
        )
    )
    challenge_configs = {cc.challenge_id: cc for cc in cc_result.scalars().all()}

    # 2) 대회 내 풀이 수 집계 (챌린지별) — freeze 필터 적용
    solve_counts_result = await db.execute(
        select(
            CompetitionSolve.challenge_id,
            func.count().label("solve_count"),
        )
        .where(*base_solve_filter)
        .group_by(CompetitionSolve.challenge_id)
    )
    solve_counts = {row.challenge_id: row.solve_count for row in solve_counts_result.all()}

    # 3) 챌린지별 현재 점수 계산
    challenge_values = competition_challenge_values(challenge_configs, solve_counts)

    # 4) 참가자별 풀이 목록 — freeze 필터 적용
    solves_result = await db.execute(
        select(CompetitionSolve.user_id, CompetitionSolve.challenge_id, CompetitionSolve.solved_at)
        .where(*base_solve_filter)
    )
    solves = [(str(row.user_id), row.challenge_id, row.solved_at) for row in solves_result.all()]

    # 5) 참가자 목록 + 점수 합산
    participants_result = await db.execute(
        select(CompetitionParticipant, User.username)
        .join(User, User.id == CompetitionParticipant.user_id)
        .where(CompetitionParticipant.competition_id == comp_id)
    )
    subjects = [(str(participant.user_id), username) for participant, username in participants_result.all()]
    entries = [{"rank": row["rank"], "user_id": row["subject_id"], "username": row["name"],
                "score": row["score"], "solves": row["solves"], "first_bloods": row["first_bloods"]}
               for row in competition_scoreboard_entries(subjects, solves, challenge_values)]

    return {
        "entries": entries,
        "is_frozen": is_frozen,
    }


@router.get("/competitions/{comp_id}/scoreboard/graph")
async def scoreboard_graph(
    comp_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """스코어보드 시계열 그래프 데이터 — 상위 10명 누적 점수 추이."""
    comp_result = await db.execute(select(Competition).where(Competition.id == comp_id))
    comp = comp_result.scalar_one_or_none()
    if not comp or comp.is_deleted or not comp.is_published:
        raise HTTPException(status_code=404, detail="대회를 찾을 수 없습니다.")

    if team_service.is_fixed_roster_team(comp):
        return await _team_score_response(db, comp, graph=True)
    is_frozen, freeze_start = competition_freeze_window(comp.end_at, comp.freeze_minutes, datetime.now(timezone.utc))

    graph_solve_filter = [CompetitionSolve.competition_id == comp_id]
    if is_frozen:
        graph_solve_filter.append(CompetitionSolve.solved_at < freeze_start)

    # 1) 대회 챌린지 설정
    cc_result = await db.execute(
        select(CompetitionChallenge).where(CompetitionChallenge.competition_id == comp_id)
    )
    challenge_configs = {cc.challenge_id: cc for cc in cc_result.scalars().all()}

    # 2) 챌린지별 현재 풀이 수 → 동적 점수 계산 — freeze 필터 적용
    solve_counts_result = await db.execute(
        select(CompetitionSolve.challenge_id, func.count().label("cnt"))
        .where(*graph_solve_filter)
        .group_by(CompetitionSolve.challenge_id)
    )
    solve_counts = {row.challenge_id: row.cnt for row in solve_counts_result.all()}

    # NOTE: 동적 점수는 현재 시점의 풀이 수를 기준으로 계산합니다.
    # 역사적 시점별 정확한 점수를 계산하려면 각 이벤트 시점의 풀이 수가 필요합니다.
    # 현재는 단순화된 근사값을 사용합니다.
    challenge_values = competition_challenge_values(challenge_configs, solve_counts)

    # 3) 모든 풀이 이벤트 (시간순) — freeze 필터 적용
    solves_result = await db.execute(
        select(
            CompetitionSolve.user_id,
            CompetitionSolve.challenge_id,
            CompetitionSolve.solved_at,
        )
        .where(*graph_solve_filter)
        .order_by(CompetitionSolve.solved_at)
    )
    all_solves = solves_result.all()

    # 4) 사용자 이름 매핑
    user_id_set = {row.user_id for row in all_solves}
    username_map: dict[str, str] = {}
    if user_id_set:
        users_result = await db.execute(
            select(User.id, User.username).where(User.id.in_(list(user_id_set)))
        )
        username_map = {str(u.id): u.username for u in users_result.all()}

    solves = [(str(row.user_id), row.challenge_id, row.solved_at) for row in all_solves]
    series = competition_scoreboard_series(comp.start_at, solves, challenge_values, username_map)
    return {"series": [{"username": row["name"], "data": row["data"]} for row in series]}


# ──── 대회 챌린지 관리 ────


class AddChallengeRequest(BaseModel):
    challenge_id: int
    category: str
    difficulty: str
    scoring_type: str = Field("static", pattern=r"^(static|dynamic)$")
    initial_value: int = Field(500, ge=1)
    decay: int = Field(100, ge=1)
    minimum_value: int = Field(50, ge=0)


@router.get("/competitions/{comp_id}/challenges")
async def list_competition_challenges(
    comp_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "moderator")),
):
    """대회에 등록된 챌린지 목록 (관리자용)."""
    await _require_active_competition(db, comp_id)
    result = await db.execute(
        select(CompetitionChallenge).where(
            CompetitionChallenge.competition_id == comp_id
        )
    )
    items = result.scalars().all()
    return [
        {
            "challenge_id": c.challenge_id,
            "category": c.category,
            "difficulty": c.difficulty,
            "scoring_type": c.scoring_type,
            "initial_value": c.initial_value,
            "decay": c.decay,
            "minimum_value": c.minimum_value,
        }
        for c in items
    ]


@router.post("/competitions/{comp_id}/challenges", status_code=201)
async def add_competition_challenge(
    comp_id: UUID,
    body: AddChallengeRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "moderator")),
):
    """대회에 챌린지 추가."""
    await _require_active_competition(db, comp_id)

    await _require_publishable_competition_challenge(body.challenge_id)

    existing = await db.execute(
        select(CompetitionChallenge).where(
            CompetitionChallenge.competition_id == comp_id,
            CompetitionChallenge.challenge_id == body.challenge_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="이미 추가된 챌린지입니다.")

    cc = CompetitionChallenge(
        competition_id=comp_id,
        challenge_id=body.challenge_id,
        category=body.category,
        difficulty=body.difficulty,
        scoring_type=body.scoring_type,
        initial_value=body.initial_value,
        decay=body.decay,
        minimum_value=body.minimum_value,
    )
    db.add(cc)
    await db.commit()
    return {"message": "챌린지가 추가되었습니다."}


@router.delete("/competitions/{comp_id}/challenges/{challenge_id}")
async def remove_competition_challenge(
    comp_id: UUID,
    challenge_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "moderator")),
):
    """대회에서 챌린지 제거."""
    await _require_active_competition(db, comp_id)
    result = await db.execute(
        delete(CompetitionChallenge).where(
            CompetitionChallenge.competition_id == comp_id,
            CompetitionChallenge.challenge_id == challenge_id,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")
    await db.commit()
    return {"message": "챌린지가 제거되었습니다."}


@router.get(
    "/competitions/{comp_id}/deletion-impact",
    response_model=CompetitionDeletionImpactResponse,
)
async def get_competition_deletion_impact(
    comp_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """보관 삭제 전에 보존될 참가·풀이·배정·문제 매핑 수를 표시한다."""
    result = await db.execute(select(Competition).where(Competition.id == comp_id))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="대회를 찾을 수 없습니다.")
    return CompetitionDeletionImpactResponse(
        target_id=comp.id,
        target_name=comp.title,
        is_deleted=comp.is_deleted,
        related_counts=await _competition_deletion_impact(db, comp.id),
        recovery_available=comp.is_deleted,
        deleted_at=comp.deleted_at,
    )


@router.post(
    "/competitions/{comp_id}/deletion-confirmations",
    response_model=CompetitionDeletionConfirmationResponse,
)
async def create_competition_deletion_confirmation(
    comp_id: UUID,
    body: CompetitionDeletionConfirmationRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """2단계 확인을 통과한 관리자에게 10분짜리 1회용 최종 삭제 토큰을 발급한다."""
    # Every deletion-generation mutation locks the parent first.  This keeps
    # confirmation issuance from committing across a concurrent delete/restore
    # boundary and gives delete/restore one consistent lock order.
    result = await db.execute(
        select(Competition).where(Competition.id == comp_id).with_for_update()
    )
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="대회를 찾을 수 없습니다.")
    if comp.is_deleted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 삭제된 대회입니다.")
    if body.target_name != comp.title:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="최종 확인 대상명이 일치하지 않습니다.")
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="삭제 사유를 입력하세요.")

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + _DELETION_CONFIRMATION_TTL
    db.add(
        CompetitionDeletionConfirmation(
            competition_id=comp.id,
            actor_id=user.id,
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            reason=reason,
            expires_at=expires_at,
        )
    )
    await db.commit()
    return CompetitionDeletionConfirmationResponse(confirmation_token=token, expires_at=expires_at)


@router.post(
    "/competitions/{comp_id}/restore",
    response_model=CompetitionDeletionActionResponse,
)
async def restore_competition(
    comp_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """대회와 보존된 참가·풀이·배정·문제 매핑을 다시 공개 가능 상태로 복구한다."""
    result = await db.execute(
        select(Competition).where(Competition.id == comp_id).with_for_update()
    )
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="대회를 찾을 수 없습니다.")
    if not comp.is_deleted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="삭제되지 않은 대회입니다.")

    prior_reason = comp.deletion_reason
    now = datetime.now(timezone.utc)
    invalidated_tokens = await db.execute(
        update(CompetitionDeletionConfirmation)
        .where(
            CompetitionDeletionConfirmation.competition_id == comp_id,
            CompetitionDeletionConfirmation.used_at.is_(None),
        )
        .values(used_at=now)
    )
    comp.is_deleted = False
    comp.is_published = comp.published_before_deletion
    comp.published_before_deletion = False
    comp.deleted_at = None
    comp.deleted_by = None
    comp.deletion_reason = None
    await record_audit(
        db,
        actor_id=str(user.id),
        action="competition.restore",
        target_type="competition",
        target_id=str(comp.id),
        details={
            "previous_deletion_reason": prior_reason,
            "invalidated_confirmation_tokens": max(
                invalidated_tokens.rowcount or 0,
                0,
            ),
        },
        request=request,
    )
    await db.commit()
    return CompetitionDeletionActionResponse(
        message="대회를 복구했습니다.",
        target_id=comp.id,
        is_deleted=False,
        recovery_available=False,
    )


@router.delete("/competitions/{comp_id}", response_model=CompetitionDeletionActionResponse)
async def delete_competition(
    comp_id: UUID,
    body: CompetitionDeletionExecuteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """3단계 확인을 거친 대회를 보관 삭제한다.

    참가·풀이·배정·점수판·문제 매핑을 물리 삭제하지 않으므로, 복구와 감사가
    가능하다. 영구 삭제는 별도 보존기간/백업 검증 절차가 확정되기 전까지 제공하지 않는다.
    """
    now = datetime.now(timezone.utc)
    token_hash = hashlib.sha256(body.confirmation_token.encode("utf-8")).hexdigest()
    # Probe without a row lock so invalid or cross-target tokens retain the
    # existing fail-closed response.  Mutating locks below always take the
    # Competition row before the confirmation row.
    confirmation_probe_result = await db.execute(
        select(
            CompetitionDeletionConfirmation.id,
            CompetitionDeletionConfirmation.competition_id,
            CompetitionDeletionConfirmation.actor_id,
        )
        .where(CompetitionDeletionConfirmation.token_hash == token_hash)
    )
    confirmation_probe = confirmation_probe_result.one_or_none()
    if not confirmation_probe:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="유효한 삭제 확인 토큰이 아닙니다.")
    if confirmation_probe.competition_id != comp_id or confirmation_probe.actor_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="삭제 확인 토큰의 대상 또는 행위자가 일치하지 않습니다.")

    result = await db.execute(
        select(Competition).where(Competition.id == comp_id).with_for_update()
    )
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="대회를 찾을 수 없습니다.")

    confirmation_result = await db.execute(
        select(CompetitionDeletionConfirmation)
        .where(CompetitionDeletionConfirmation.id == confirmation_probe.id)
        .with_for_update()
    )
    confirmation = confirmation_result.scalar_one_or_none()
    if not confirmation:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="유효한 삭제 확인 토큰이 아닙니다.")
    if confirmation.competition_id != comp_id or confirmation.actor_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="삭제 확인 토큰의 대상 또는 행위자가 일치하지 않습니다.")
    if confirmation.used_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 사용한 삭제 확인 토큰입니다.")
    if confirmation.expires_at <= now:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="삭제 확인 토큰이 만료되었습니다.")
    if comp.is_deleted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 삭제된 대회입니다.")

    impact = await _competition_deletion_impact(db, comp.id)
    confirmation.used_at = now
    comp.published_before_deletion = comp.is_published
    comp.is_published = False
    comp.is_deleted = True
    comp.deleted_at = now
    comp.deleted_by = user.id
    comp.deletion_reason = confirmation.reason
    await record_audit(
        db,
        actor_id=str(user.id),
        action="competition.soft_delete",
        target_type="competition",
        target_id=str(comp.id),
        details={"reason": confirmation.reason, "impact": impact},
        request=request,
    )
    await db.commit()
    return CompetitionDeletionActionResponse(
        message="대회를 보관 삭제했습니다.",
        target_id=comp.id,
        is_deleted=True,
        recovery_available=True,
    )
