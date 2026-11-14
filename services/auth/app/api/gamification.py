"""게이미피케이션 API — 뱃지 조건 검사 + 레벨 조회."""

import asyncio
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.competition import Competition, CompetitionParticipant
from app.api.deps import get_current_user
from app.utils.internal_api import service_get

logger = logging.getLogger(__name__)

router = APIRouter(tags=["게이미피케이션"])


# ──── 뱃지 정의 ────
# hidden=True인 뱃지는 획득 전까지 프론트엔드에서 내용이 숨겨짐 (+(숫자) 표시)
# target: 진행도 목표값 (None이면 진행도 표시 안 함 — 조건이 yes/no인 뱃지)

BADGE_DEFINITIONS = [
    # ── 풀이 마일스톤 ──
    {"id": "first_blood", "name": "First Blood", "description": "첫 번째 문제 풀이", "icon": "🩸", "hidden": False, "target": 1},
    {"id": "solver_10", "name": "문제 사냥꾼", "description": "문제 10개 풀이", "icon": "🎯", "hidden": False, "target": 10},
    {"id": "solver_50", "name": "풀이 장인", "description": "문제 50개 풀이", "icon": "🏹", "hidden": False, "target": 50},
    {"id": "solver_100", "name": "백전백승", "description": "문제 100개 풀이", "icon": "💯", "hidden": False, "target": 100},

    # ── 카테고리 전문가 ──
    {"id": "web_explorer", "name": "Web 탐험가", "description": "Web 문제 5개 풀이", "icon": "🌐", "hidden": False, "target": 5},
    {"id": "web_master", "name": "Web 마스터", "description": "Web 문제 20개 풀이", "icon": "🕸️", "hidden": False, "target": 20},
    {"id": "pwn_starter", "name": "Pwn 입문", "description": "Pwn 문제 3개 풀이", "icon": "💥", "hidden": False, "target": 3},
    {"id": "pwn_master", "name": "Pwn 마스터", "description": "Pwn 문제 15개 풀이", "icon": "🔓", "hidden": False, "target": 15},
    {"id": "rev_starter", "name": "Rev 입문", "description": "Reversing 문제 3개 풀이", "icon": "🔍", "hidden": False, "target": 3},
    {"id": "rev_master", "name": "Rev 마스터", "description": "Reversing 문제 15개 풀이", "icon": "🧬", "hidden": False, "target": 15},
    {"id": "crypto_starter", "name": "Crypto 입문", "description": "Crypto 문제 3개 풀이", "icon": "🔐", "hidden": False, "target": 3},
    {"id": "crypto_master", "name": "Crypto 마스터", "description": "Crypto 문제 15개 풀이", "icon": "🗝️", "hidden": False, "target": 15},
    {"id": "forensic_starter", "name": "Forensic 입문", "description": "Forensic 문제 3개 풀이", "icon": "🔬", "hidden": False, "target": 3},
    {"id": "forensic_master", "name": "Forensic 마스터", "description": "Forensic 문제 15개 풀이", "icon": "🕵️", "hidden": False, "target": 15},
    {"id": "all_rounder", "name": "올라운더", "description": "모든 카테고리 1문제 이상 풀이", "icon": "🎖️", "hidden": False, "target": 6},

    # ── 연속 출석 ──
    {"id": "streak_3", "name": "3일 연속", "description": "3일 연속 활동", "icon": "🔥", "hidden": False, "target": 3},
    {"id": "streak_7", "name": "주간 전사", "description": "7일 연속 활동", "icon": "⚔️", "hidden": False, "target": 7},
    {"id": "streak_14", "name": "2주 연속", "description": "14일 연속 활동", "icon": "🛡️", "hidden": False, "target": 14},
    {"id": "streak_30", "name": "월간 챔피언", "description": "30일 연속 활동", "icon": "👑", "hidden": False, "target": 30},

    # ── 레벨 ──
    {"id": "level_5", "name": "레벨 5 달성", "description": "레벨 5 도달", "icon": "⭐", "hidden": False, "target": 5},
    {"id": "level_10", "name": "레벨 10 달성", "description": "레벨 10 도달", "icon": "🌟", "hidden": False, "target": 10},
    {"id": "level_20", "name": "레벨 20 달성", "description": "레벨 20 도달", "icon": "✨", "hidden": False, "target": 20},

    # ── 커뮤니티/학습 ──
    {"id": "learner", "name": "학습자", "description": "첫 코스 완료", "icon": "📚", "hidden": False, "target": 1},
    {"id": "community_star", "name": "커뮤니티 스타", "description": "게시글 좋아요 10개 이상", "icon": "💫", "hidden": False, "target": 10},
    {"id": "first_post", "name": "첫 게시글", "description": "첫 커뮤니티 게시글 작성", "icon": "📝", "hidden": False, "target": 1},
    {"id": "competition_join", "name": "대회 참가자", "description": "첫 대회 참가", "icon": "🏁", "hidden": False, "target": 1},

    # ── 히든 뱃지 (획득 전까지 숨김) ──
    {"id": "night_owl", "name": "올빼미", "description": "새벽 3시~5시에 문제 풀이", "icon": "🦉", "hidden": True, "target": None},
    {"id": "speed_demon", "name": "번개 풀이", "description": "문제를 60초 내에 풀이", "icon": "⚡", "hidden": True, "target": None},
    {"id": "perfectionist", "name": "완벽주의자", "description": "한 카테고리 전체 풀이", "icon": "💎", "hidden": True, "target": None},
    {"id": "early_bird", "name": "얼리버드", "description": "문제 출제 후 1시간 내 풀이", "icon": "🐦", "hidden": True, "target": None},
    {"id": "streak_100", "name": "전설의 출석", "description": "100일 연속 활동", "icon": "🏆", "hidden": True, "target": 100},
    {"id": "xp_10000", "name": "만렙을 향해", "description": "누적 XP 10,000 달성", "icon": "🚀", "hidden": True, "target": 10000},
    {"id": "helping_hand", "name": "도우미", "description": "커뮤니티 답변 채택 5회", "icon": "🤝", "hidden": True, "target": None},
    {"id": "team_player", "name": "팀 플레이어", "description": "팀 대회 참가 후 입상", "icon": "🎪", "hidden": True, "target": None},
]

# 뱃지 ID → 정의 매핑 (빠른 조회용)
_BADGE_MAP = {b["id"]: b for b in BADGE_DEFINITIONS}

# 모든 카테고리 목록
ALL_CATEGORIES = ["web", "pwn", "rev", "crypto", "forensic", "misc"]


class BadgeResponse(BaseModel):
    id: str
    name: str
    description: str
    icon: str
    earned: bool
    hidden: bool
    progress: int | None = None   # 현재 진행도 (None이면 표시 안 함)
    target: int | None = None     # 목표값 (None이면 표시 안 함)


class LevelInfo(BaseModel):
    level: int
    xp: int
    xp_to_next: int
    streak_days: int


# XP 레벨 테이블 (레벨당 필요 XP)
XP_TABLE = [0, 100, 250, 500, 1000, 1800, 3000, 5000, 8000, 12000, 18000]


def get_xp_for_level(level: int) -> int:
    """해당 레벨까지 필요한 총 XP."""
    if level < len(XP_TABLE):
        return XP_TABLE[level]
    return XP_TABLE[-1] + (level - len(XP_TABLE) + 1) * 5000


def _evaluate_badges(
    user: User,
    total_solves: int,
    category_solves: dict[str, int],
    learn_badge_data: dict | None,
    community_stats: dict | None,
    competition_count: int,
    team_competition_placed: bool = False,
) -> list[BadgeResponse]:
    """전체 뱃지 평가 후 결과 리스트 반환."""
    cat_solves = category_solves
    badge_data = learn_badge_data or {}
    comm = community_stats or {}

    course_completions = badge_data.get("course_completions", 0)
    has_night_solve = badge_data.get("has_night_solve", False)
    has_early_bird = badge_data.get("has_early_bird", False)
    has_speed_solve = badge_data.get("has_speed_solve", False)
    perfectionist_cats = badge_data.get("perfectionist_categories", [])

    post_count = comm.get("post_count", 0)
    max_like_count = comm.get("max_like_count", 0)
    helpful_count = comm.get("helpful_count", 0)

    # 뱃지별 (earned, progress) 계산
    # progress가 None이면 진행도 표시 안 함 (조건이 yes/no인 히든 뱃지)
    evaluation: dict[str, tuple[bool, int | None]] = {}

    # ── 풀이 마일스톤 ──
    evaluation["first_blood"] = (total_solves >= 1, min(total_solves, 1))
    evaluation["solver_10"] = (total_solves >= 10, min(total_solves, 10))
    evaluation["solver_50"] = (total_solves >= 50, min(total_solves, 50))
    evaluation["solver_100"] = (total_solves >= 100, min(total_solves, 100))

    # ── 카테고리 전문가 ──
    web = cat_solves.get("web", 0)
    evaluation["web_explorer"] = (web >= 5, min(web, 5))
    evaluation["web_master"] = (web >= 20, min(web, 20))

    pwn = cat_solves.get("pwn", 0)
    evaluation["pwn_starter"] = (pwn >= 3, min(pwn, 3))
    evaluation["pwn_master"] = (pwn >= 15, min(pwn, 15))

    rev = cat_solves.get("rev", 0)
    evaluation["rev_starter"] = (rev >= 3, min(rev, 3))
    evaluation["rev_master"] = (rev >= 15, min(rev, 15))

    crypto = cat_solves.get("crypto", 0)
    evaluation["crypto_starter"] = (crypto >= 3, min(crypto, 3))
    evaluation["crypto_master"] = (crypto >= 15, min(crypto, 15))

    forensic = cat_solves.get("forensic", 0)
    evaluation["forensic_starter"] = (forensic >= 3, min(forensic, 3))
    evaluation["forensic_master"] = (forensic >= 15, min(forensic, 15))

    # 올라운더: 6개 카테고리 모두 1개 이상
    cats_with_solve = sum(1 for c in ALL_CATEGORIES if cat_solves.get(c, 0) >= 1)
    evaluation["all_rounder"] = (cats_with_solve >= 6, min(cats_with_solve, 6))

    # ── 연속 출석 ──
    streak = user.streak_days
    evaluation["streak_3"] = (streak >= 3, min(streak, 3))
    evaluation["streak_7"] = (streak >= 7, min(streak, 7))
    evaluation["streak_14"] = (streak >= 14, min(streak, 14))
    evaluation["streak_30"] = (streak >= 30, min(streak, 30))
    evaluation["streak_100"] = (streak >= 100, min(streak, 100))

    # ── 레벨 ──
    level = user.level
    evaluation["level_5"] = (level >= 5, min(level, 5))
    evaluation["level_10"] = (level >= 10, min(level, 10))
    evaluation["level_20"] = (level >= 20, min(level, 20))

    # ── XP 마일스톤 ──
    xp = user.xp
    evaluation["xp_10000"] = (xp >= 10000, min(xp, 10000))

    # ── 커뮤니티/학습 ──
    evaluation["learner"] = (course_completions >= 1, min(course_completions, 1))
    evaluation["community_star"] = (max_like_count >= 10, min(max_like_count, 10))
    evaluation["first_post"] = (post_count >= 1, min(post_count, 1))
    evaluation["competition_join"] = (competition_count >= 1, min(competition_count, 1))

    # ── 히든 뱃지 (yes/no — progress 없음) ──
    evaluation["night_owl"] = (has_night_solve, None)
    evaluation["early_bird"] = (has_early_bird, None)
    evaluation["perfectionist"] = (len(perfectionist_cats) > 0, None)

    # ── 히든 뱃지 (데이터 기반) ──
    # speed_demon: 60초 이내 풀이 또는 어떤 챌린지의 first blood
    evaluation["speed_demon"] = (has_speed_solve, None)
    # helping_hand: QnA 게시판에서 타인의 질문에 답변 5회 이상
    evaluation["helping_hand"] = (helpful_count >= 5, None)
    # team_player: 팀 대회에서 점수를 획득한 경험 (입상)
    evaluation["team_player"] = (team_competition_placed, None)

    # 결과 빌드
    results: list[BadgeResponse] = []
    for badge_def in BADGE_DEFINITIONS:
        bid = badge_def["id"]
        earned, progress = evaluation.get(bid, (False, None))
        target = badge_def.get("target")

        results.append(BadgeResponse(
            id=bid,
            name=badge_def["name"],
            description=badge_def["description"],
            icon=badge_def["icon"],
            earned=earned,
            hidden=badge_def["hidden"],
            progress=progress,
            target=target,
        ))

    return results


async def evaluate_user_badges(user: User, db: AsyncSession):
    """뱃지 목록 (획득 여부 + 진행도 포함)."""
    uid = str(user.id)

    # learn-service + community-service 병렬 호출 (asyncio.gather)
    learn_url = settings.learn_service_url
    community_url = settings.community_service_url

    badge_data, community_stats = await asyncio.gather(
        service_get(learn_url, f"/internal/users/{uid}/badge-data"),
        service_get(community_url, f"/internal/users/{uid}/badge-stats"),
    )

    # badge-data에서 category_solves 추출 → total_solves 계산 (별도 /summary 호출 불필요)
    category_solves = badge_data.get("category_solves", {}) if badge_data else {}
    total_solves = sum(category_solves.values())

    # auth-service DB: 대회 참가 수
    comp_result = await db.execute(
        select(func.count())
        .select_from(CompetitionParticipant)
        .join(Competition, Competition.id == CompetitionParticipant.competition_id)
        .where(
            CompetitionParticipant.user_id == user.id,
            Competition.is_deleted.is_(False),
        )
    )
    competition_count = comp_result.scalar() or 0

    # auth-service DB: 팀 대회에서 점수를 획득한 경험 (team_player 뱃지용)
    team_placed_result = await db.execute(
        select(func.count())
        .select_from(CompetitionParticipant)
        .join(Competition, Competition.id == CompetitionParticipant.competition_id)
        .where(
            CompetitionParticipant.user_id == user.id,
            Competition.mode == "team",
            Competition.is_deleted.is_(False),
            CompetitionParticipant.score > 0,
        )
    )
    team_competition_placed = (team_placed_result.scalar() or 0) > 0

    badges = _evaluate_badges(
        user=user,
        total_solves=total_solves,
        category_solves=category_solves,
        learn_badge_data=badge_data,
        community_stats=community_stats,
        competition_count=competition_count,
        team_competition_placed=team_competition_placed,
    )
    return badges, badge_data is not None and community_stats is not None


async def observe_user_badges(user: User, db: AsyncSession):
    badges, complete = await evaluate_user_badges(user, db)
    if complete:
        from app.utils.personal_notifications import record_badge_observations
        await record_badge_observations(db, user, badges)
        await db.commit()
    return badges, complete


@router.get("/badges", response_model=list[BadgeResponse])
async def list_badges(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    badges, _complete = await observe_user_badges(user, db)
    return badges


@router.get("/level", response_model=LevelInfo)
async def get_level_info(user: User = Depends(get_current_user)):
    """레벨/XP 정보 조회."""
    current_xp = user.xp
    current_level = user.level
    next_level_xp = get_xp_for_level(current_level + 1)

    return LevelInfo(
        level=current_level,
        xp=current_xp,
        xp_to_next=max(0, next_level_xp - current_xp),
        streak_days=user.streak_days,
    )
