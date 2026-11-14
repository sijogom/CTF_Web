"""사용자 본인 정보 조회/수정 API."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.config import settings
from app.database import get_db
from app.models.user import User
from app.schemas.auth import UserProfile
from app.utils.internal_api import service_get
from app.utils.minio_client import (
    upload_avatar, delete_avatar, AVATAR_MAX_SIZE, AvatarValidationError,
)
from app.utils.safe_html import (
    RichContentError,
    normalize_rich_content_for_write,
    sanitize_rich_content,
)

router = APIRouter(tags=["유저"])
logger = logging.getLogger(__name__)


# ──── 기본 정보 ────


async def _get_user_tier(xp: int, tier_override: str | None = None) -> dict | None:
    """admin:settings의 tiers 배열에서 티어를 반환. tier_override가 있으면 해당 이름 우선."""
    try:
        from app.redis import redis_client
        raw = await redis_client.get("admin:settings")
        if not raw:
            return None
        data = json.loads(raw)
        tiers = data.get("tiers")
        if not tiers or not isinstance(tiers, list):
            return None
        # 강제 설정된 티어가 있으면 이름으로 매칭
        if tier_override:
            for tier in tiers:
                if tier.get("name") == tier_override:
                    return tier
            return None
        # XP 기반 자동 매칭
        matched = None
        for tier in sorted(tiers, key=lambda t: t.get("required_xp", 0)):
            if xp >= tier.get("required_xp", 0):
                matched = tier
        return matched
    except Exception:
        return None


@router.get("/me", response_model=UserProfile)
async def get_me(user: User = Depends(get_current_user)):
    tier = await _get_user_tier(user.xp, user.tier_override)
    return UserProfile(
        id=str(user.id),
        email=user.email,
        username=user.username,
        role=user.role,
        level=user.level,
        xp=user.xp,
        points=user.points,
        streak_days=user.streak_days,
        avatar_url=user.avatar_url,
        bio=user.bio,
        email_verified=user.email_verified,
        email_notifications=user.email_notifications,
        totp_enabled=user.totp_enabled,
        discord_id=user.discord_id,
        discord_username=user.discord_username,
        baekjoon_handle=user.baekjoon_handle,
        baekjoon_tier=user.baekjoon_tier,
        tier_name=tier.get("name") if tier else None,
        tier_image=tier.get("image_url") if tier else None,
    )


class ProfileUpdateRequest(BaseModel):
    username: str | None = Field(None, min_length=3, max_length=50, pattern=r"^[\w가-힣\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF-]+$")
    bio: str | None = None

    @field_validator("bio")
    @classmethod
    def sanitize_bio(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_rich_content(
            value,
            field_name="자기소개",
            allow_empty=True,
            max_visible_chars=500,
        )


@router.patch("/me")
async def update_profile(
    body: ProfileUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """프로필 수정 (닉네임, 자기소개)."""
    if body.bio is None and user.bio is not None:
        try:
            user.bio = normalize_rich_content_for_write(
                user.bio,
                field_name="자기소개",
                allow_empty=True,
                max_visible_chars=500,
            )
        except RichContentError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=error.as_detail(),
            ) from error
    if body.username is not None and body.username != user.username:
        # 닉네임 중복 체크
        existing = await db.execute(
            select(User.id).where(User.username == body.username, User.id != user.id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="이미 사용 중인 닉네임입니다.",
            )
        user.username = body.username

    if body.bio is not None:
        user.bio = body.bio

    await db.commit()
    return {"message": "프로필이 수정되었습니다.", "username": user.username, "bio": user.bio or ""}


async def _replace_avatar_pointer(db: AsyncSession, user_id, avatar_url: str | None) -> str | None:
    try:
        result = await db.execute(
            select(User).where(User.id == user_id).with_for_update()
            .execution_options(populate_existing=True)
        )
        current_user = result.scalar_one_or_none()
        if current_user is None or not current_user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유저를 찾을 수 없습니다.")
        old_url = current_user.avatar_url
        current_user.avatar_url = avatar_url
        await db.commit()
        return old_url
    except BaseException as error:
        # commit 응답 유실은 rollback 성공만으로 미적용이라 단정할 수 없다.
        try:
            await db.rollback()
        except BaseException:
            logger.warning("프로필 사진 변경 후 DB 상태 확인이 필요합니다.")
        if not isinstance(error, Exception) or isinstance(error, HTTPException):
            raise
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="프로필 사진 저장 결과를 확인할 수 없습니다. 프로필을 다시 조회해주세요.",
        ) from None


@router.post("/me/avatar")
async def update_avatar(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """프로필 사진 업로드/변경."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="파일이 없습니다.")

    data = await file.read(AVATAR_MAX_SIZE + 1)
    if len(data) > AVATAR_MAX_SIZE:
        raise HTTPException(status_code=400, detail="파일 크기가 2MB를 초과합니다.")

    try:
        object_path = upload_avatar(data, file.filename, str(user.id))
    except AvatarValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="프로필 사진 업로드 결과를 확인할 수 없습니다. 프로필을 다시 조회해주세요.",
        ) from None

    import os
    avatar_url = f"{os.environ.get('COOKIE_BASE_PATH', '')}/storage/{object_path}"
    old_url = await _replace_avatar_pointer(db, user.id, avatar_url)
    if old_url != avatar_url:
        delete_avatar(old_url, str(user.id))
    return {"avatar_url": avatar_url}


@router.delete("/me/avatar")
async def remove_avatar(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """프로필 사진 삭제."""
    old_url = await _replace_avatar_pointer(db, user.id, None)
    delete_avatar(old_url, str(user.id))
    return {"avatar_url": None}


@router.patch("/me/email-preferences")
async def update_email_preferences(
    enabled: bool = Query(..., description="이메일 수신 여부"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """이메일 수신 설정 변경."""
    user.email_notifications = enabled
    await db.commit()
    return {"email_notifications": enabled}


class NotificationPreferencesPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: StrictBool | None = None
    competition: StrictBool | None = None
    badge: StrictBool | None = None
    email: StrictBool | None = None

    @field_validator("comment", "competition", "badge", "email")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("알림 설정은 true 또는 false여야 합니다.")
        return value


def notification_preferences(user: User) -> dict[str, bool]:
    return {
        "comment": user.comment_notifications,
        "competition": user.competition_notifications,
        "badge": user.badge_notifications,
        "email": user.email_notifications,
    }


@router.get("/me/notification-preferences")
async def get_notification_preferences(user: User = Depends(get_current_user)):
    return notification_preferences(user)


@router.patch("/me/notification-preferences")
async def update_notification_preferences(
    body: NotificationPreferencesPatch,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    columns = {"comment": "comment_notifications", "competition": "competition_notifications",
               "badge": "badge_notifications", "email": "email_notifications"}
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(user, columns[key], value)
    await db.commit()
    return notification_preferences(user)


@router.get("/me/points")
async def get_my_points(user: User = Depends(get_current_user)):
    """내 포인트 잔액 조회."""
    return {"points": user.points}


@router.delete("/users/me")
async def delete_account(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """계정 소프트 삭제 (is_active = False) + 세션 무효화."""
    user.is_active = False
    await db.commit()

    # 해당 사용자의 리프레시 토큰 일괄 무효화
    from app.api.refresh import _revoke_all_user_tokens
    try:
        await _revoke_all_user_tokens(str(user.id))
    except Exception:
        pass  # Redis 장애 시에도 계정 삭제 자체는 완료

    return {"message": "계정이 삭제되었습니다."}


# ──── 프로필 상세 데이터 ────


@router.get("/users/me/profile")
async def get_profile(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """프로필 상세 조회 (레벨, XP, 풀이 수, 랭킹)."""
    uid = str(user.id)

    # learn-service에서 풀이 수 조회
    learn_url = settings.learn_service_url
    summary = await service_get(learn_url, f"/internal/users/{uid}/summary")
    total_solves = summary.get("total_solves", 0) if isinstance(summary, dict) else 0

    # 랭킹 계산: 나보다 XP가 높은 활성 유저 수 + 1
    rank_result = await db.execute(
        select(func.count()).select_from(User).where(
            User.is_active.is_(True),
            User.role.notin_(["admin", "moderator"]),
            User.xp > user.xp,
        )
    )
    rank = (rank_result.scalar() or 0) + 1

    from app.api.gamification import get_xp_for_level
    current_level_xp = 0 if user.level <= 1 else get_xp_for_level(user.level)
    next_level_xp = get_xp_for_level(user.level + 1)
    xp_in_level = user.xp - current_level_xp
    xp_for_level = next_level_xp - current_level_xp
    return {
        "username": user.username,
        "bio": user.bio or "",
        "avatar_url": user.avatar_url,
        "level": user.level,
        "xp": user.xp,
        "xp_in_level": max(0, xp_in_level),
        "xp_for_level": max(1, xp_for_level),
        "xp_to_next": max(0, next_level_xp - user.xp),
        "streak_days": user.streak_days,
        "total_solves": total_solves,
        "rank": rank,
        "join_date": user.created_at.isoformat() if user.created_at else None,
        "discord_username": user.discord_username,
        "baekjoon_handle": user.baekjoon_handle,
        "baekjoon_tier": user.baekjoon_tier,
        "tier_name": (await _get_user_tier(user.xp, user.tier_override) or {}).get("name"),
        "tier_image": (await _get_user_tier(user.xp, user.tier_override) or {}).get("image_url"),
    }


@router.get("/users/{user_id}/public-profile")
async def get_public_profile(
    user_id: str,
    db: AsyncSession = Depends(get_db),
):
    """다른 사용자의 공개 프로필 조회 (인증 불필요)."""
    import uuid as _uuid

    # UUID 형식 검증
    try:
        uid = str(_uuid.UUID(user_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")

    result = await db.execute(select(User).where(User.id == uid))
    target = result.scalar_one_or_none()

    # 비활성 유저 또는 관리자는 404
    if not target or not target.is_active or target.role in ("admin", "moderator"):
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")

    # learn-service에서 풀이 수 조회
    learn_url = settings.learn_service_url
    summary = await service_get(learn_url, f"/internal/users/{uid}/summary")
    total_solves = summary.get("total_solves", 0) if isinstance(summary, dict) else 0

    # 랭킹 계산: 해당 유저보다 XP가 높은 활성 유저 수 + 1
    rank_result = await db.execute(
        select(func.count()).select_from(User).where(
            User.is_active.is_(True),
            User.role.notin_(["admin", "moderator"]),
            User.xp > target.xp,
        )
    )
    rank = (rank_result.scalar() or 0) + 1

    # 카테고리별 풀이 통계 + 뱃지 데이터 + 활동/이력 병렬 조회
    import asyncio as _asyncio
    from app.api.gamification import _evaluate_badges
    from app.models.competition import Competition, CompetitionParticipant

    _results = await _asyncio.gather(
        service_get(learn_url, f"/internal/users/{uid}/stats"),
        service_get(learn_url, f"/internal/users/{uid}/badge-data"),
        service_get(settings.community_service_url, f"/internal/users/{uid}/badge-stats"),
        service_get(learn_url, f"/internal/users/{uid}/history"),
        service_get(learn_url, f"/internal/users/{uid}/activity"),
        return_exceptions=True,
    )
    stats = _results[0] if not isinstance(_results[0], Exception) else None
    badge_data = _results[1] if not isinstance(_results[1], Exception) else None
    community_stats = _results[2] if not isinstance(_results[2], Exception) else None
    history = _results[3] if not isinstance(_results[3], Exception) else None
    activity = _results[4] if not isinstance(_results[4], Exception) else None

    if not stats:
        categories = ["Web", "Pwn", "Reversing", "Crypto", "Forensic", "Misc"]
        stats = [{"label": cat, "value": 0, "max": 0} for cat in categories]
    category_solves = badge_data.get("category_solves", {}) if badge_data else {}
    badge_total_solves = sum(category_solves.values())

    comp_result = await db.execute(
        select(func.count())
        .select_from(CompetitionParticipant)
        .join(Competition, Competition.id == CompetitionParticipant.competition_id)
        .where(
            CompetitionParticipant.user_id == target.id,
            Competition.is_deleted.is_(False),
        )
    )
    competition_count = comp_result.scalar() or 0

    team_placed_result = await db.execute(
        select(func.count())
        .select_from(CompetitionParticipant)
        .join(Competition, Competition.id == CompetitionParticipant.competition_id)
        .where(
            CompetitionParticipant.user_id == target.id,
            Competition.mode == "team",
            Competition.is_deleted.is_(False),
            CompetitionParticipant.score > 0,
        )
    )
    team_competition_placed = (team_placed_result.scalar() or 0) > 0

    badges = _evaluate_badges(
        user=target,
        total_solves=badge_total_solves,
        category_solves=category_solves,
        learn_badge_data=badge_data,
        community_stats=community_stats,
        competition_count=competition_count,
        team_competition_placed=team_competition_placed,
    )

    from app.api.gamification import get_xp_for_level
    current_level_xp = 0 if target.level <= 1 else get_xp_for_level(target.level)
    next_level_xp = get_xp_for_level(target.level + 1)

    return {
        "username": target.username,
        "bio": target.bio or "",
        "avatar_url": target.avatar_url,
        "level": target.level,
        "xp": target.xp,
        "xp_in_level": max(0, target.xp - current_level_xp),
        "xp_for_level": max(1, next_level_xp - current_level_xp),
        "streak_days": target.streak_days,
        "rank": rank,
        "solves": total_solves,
        "stats": stats,
        "badges": badges,
        "history": history if history is not None else [],
        "activity": activity if activity is not None else {},
        "joined_at": target.created_at.isoformat() if target.created_at else None,
        "discord_username": target.discord_username,
        "baekjoon_handle": target.baekjoon_handle,
        "baekjoon_tier": target.baekjoon_tier,
        "tier_name": (await _get_user_tier(target.xp, target.tier_override) or {}).get("name"),
        "tier_image": (await _get_user_tier(target.xp, target.tier_override) or {}).get("image_url"),
    }


@router.get("/users/me/stats")
async def get_stats(user: User = Depends(get_current_user)):
    """카테고리별 풀이 통계."""
    uid = str(user.id)
    learn_url = settings.learn_service_url
    data = await service_get(learn_url, f"/internal/users/{uid}/stats")
    if data:
        return data
    # learn-service 실패 시 빈 기본값
    categories = ["Web", "Pwn", "Reversing", "Crypto", "Forensic", "Misc"]
    return [{"label": cat, "value": 0, "max": 0} for cat in categories]


@router.get("/users/me/badges")
async def get_badges_alias(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """뱃지 목록 (gamification.py의 /badges와 동일, 경로 호환용)."""
    from app.api.gamification import list_badges
    return await list_badges(user=user, db=db)


@router.get("/users/me/history")
async def get_history(user: User = Depends(get_current_user)):
    """풀이 이력 조회."""
    uid = str(user.id)
    learn_url = settings.learn_service_url
    data = await service_get(learn_url, f"/internal/users/{uid}/history")
    return data if data is not None else []


@router.get("/users/me/activity")
async def get_activity(user: User = Depends(get_current_user)):
    """활동 히트맵 데이터."""
    uid = str(user.id)
    learn_url = settings.learn_service_url
    data = await service_get(learn_url, f"/internal/users/{uid}/activity")
    return data if data is not None else {}
